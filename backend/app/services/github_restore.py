"""Restore Bambuddy data from a Git provider backup (issue #2656).

The backup side (``github_backup.py``) is push-only: it collects a handful of
JSON documents and commits them. This module is the read side — it walks the
backup repository's history, lets a caller inspect what a given commit contains,
and applies selected categories back into the local database (or, for
K-profiles, back onto the printers).

Design notes worth knowing before editing:

* **A restore never reuses the backup's primary keys.** ``spool.id`` and
  ``print_archives.id`` are bare autoincrement columns, so the ids in a backup
  taken weeks ago very likely belong to unrelated rows today. Rows are matched
  on natural keys instead, inserted without an explicit id, and an
  ``old_id -> new_id`` map is threaded through so foreign keys in dependent
  tables (spool usage history) still line up.

  The printer-side ``cali_idx`` behaves the same way and gets the same
  treatment. Editing a K-profile in Bambuddy is a delete-then-add on a
  single-nozzle printer, which re-keys it, and ``extrusion_cali_set`` aimed at a
  slot that no longer exists is silently dropped — so the live index is read
  back and matched before writing, never taken from the backup.
* **Categories are applied archives -> spools -> settings -> kprofiles.**
  Archives first because spool usage history references ``archive_id``;
  K-profiles last because they leave the database and talk to hardware.
* **Cloud profiles are not restorable.** The backup collector never actually
  writes ``cloud_profiles/*.json``, and the preset list it would write carries
  no setting payload. Tracked separately from #2656.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import async_session
from backend.app.models.archive import PrintArchive
from backend.app.models.github_backup import GitHubBackupConfig, GitHubBackupLog
from backend.app.models.printer import Printer
from backend.app.models.project import Project
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.schemas.github_backup import RestoreCategory
from backend.app.services.git_providers.factory import get_provider_backend
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

METADATA_PATH = "backup_metadata.json"
SETTINGS_PATH = "settings/app_settings.json"
SPOOLS_PATH = "spools/inventory.json"
SPOOL_USAGE_PATH = "spools/usage_history.json"
ARCHIVES_PATH = "archives/print_history.json"

# kprofiles/{printer_serial}/{nozzle_diameter}.json
_KPROFILE_PATH_RE = re.compile(r"^kprofiles/([^/]+)/([^/]+)\.json$")

# Settings keys the backup collector already refuses to write. Applied again on
# the read side because a backup taken before that denylist existed can still
# contain them, and a restore must not resurrect a stale credential.
_SENSITIVE_SETTING_KEYS = {"bambu_cloud_token", "auth_secret_key"}

# Belt-and-braces for the same reason: any key that looks like a secret is
# skipped even if it isn't in the explicit denylist above.
_SECRET_KEY_HINTS = ("token", "secret", "password", "access_code", "api_key", "passphrase")

# Keys that decide *who can reach the instance* rather than how it behaves. The
# backup collector writes them like any other Settings row, so a backup taken
# before auth was turned on carries auth_enabled=false — and a restore reaches
# the table directly, so honouring them would:
#
#   * disable authentication outright. ``set_auth_enabled`` pairs its write with
#     ``invalidate_auth_enabled_cache()``; we cannot, so the 30 s TTL in
#     core.auth is the only thing between the write and an open instance. That
#     cache is built to fail closed — writing the stored value behind its back
#     is what would make it fail open.
#   * bypass the lockout refusals ``update_settings`` enforces (a
#     ``local_login_enabled=false`` with no enabled OIDC provider, or with no
#     OIDC link on the caller, is a 400 there — #1589).
#   * cross a permission boundary: /github-backup/restore is gated on
#     GITHUB_RESTORE alone, so this would be a way to rewrite auth config
#     without SETTINGS_UPDATE.
#
# Auth is reconfigured through the auth UI, which has the guards. Restoring it
# from a snapshot has no safe reading.
_PROTECTED_SETTING_KEYS = {
    "auth_enabled",
    "advanced_auth_enabled",
    "local_login_enabled",
    "setup_completed",
}

# Nozzle diameters the backup collector iterates. A path outside this set means
# the backup was written by a newer version, so accept it rather than dropping
# data, but keep the list for validation messages.
_KNOWN_NOZZLES = {"0.2", "0.4", "0.6", "0.8"}


def _parse_dt(value) -> datetime | None:
    """Best-effort parse of a datetime the backup wrote via ``str(...)``."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_blocked_setting_key(key: str) -> bool:
    lowered = key.lower()
    return key in _SENSITIVE_SETTING_KEYS or any(hint in lowered for hint in _SECRET_KEY_HINTS)


def _is_protected_setting_key(key: str) -> bool:
    return key in _PROTECTED_SETTING_KEYS


def _is_skipped_setting_key(key: str) -> bool:
    """True for any key ``_restore_settings`` refuses to write, for either reason."""
    return _is_blocked_setting_key(key) or _is_protected_setting_key(key)


class _CategoryTally:
    """Mutable accumulator matching ``GitHubRestoreCategoryResult``."""

    def __init__(self) -> None:
        self.restored = 0
        self.skipped = 0
        self.failed = 0
        self.notes: list[str] = []

    def note(self, message: str) -> None:
        # Notes are surfaced verbatim in the UI, so keep the list bounded rather
        # than emitting one line per row for a large backup.
        if message not in self.notes and len(self.notes) < 20:
            self.notes.append(message)

    def as_dict(self) -> dict:
        return {"restored": self.restored, "skipped": self.skipped, "failed": self.failed, "notes": self.notes}


class GitHubRestoreService:
    """Reads a backup repository and applies selected categories locally."""

    def __init__(self) -> None:
        self._running_restore: bool = False
        self._progress: str | None = None
        self._http_client: httpx.AsyncClient | None = None
        # Guards the check-then-set on ``_running_restore``. Without it two
        # concurrent POSTs can both observe False before either sets it.
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=60.0)
        return self._http_client

    @property
    def is_running(self) -> bool:
        return self._running_restore

    @property
    def progress(self) -> str | None:
        return self._progress

    # --- Repository reads --------------------------------------------------

    async def list_commits(self, config: GitHubBackupConfig, limit: int = 20) -> dict:
        """List recent commits on the configured branch."""
        backend = get_provider_backend(config.provider)
        client = await self._get_client()
        result = await backend.list_commits(
            repo_url=config.repository_url,
            token=config.access_token,
            branch=config.branch,
            client=client,
            limit=limit,
        )
        result["branch"] = config.branch
        return result

    async def _resolve_ref(self, config: GitHubBackupConfig, ref: str) -> tuple[str | None, str]:
        """Turn ``HEAD`` into a concrete commit SHA.

        Done once up front so a preview and the restore that follows it act on
        the same commit even if a scheduled backup lands in between.
        """
        if ref and ref.upper() != "HEAD":
            return ref, ""
        result = await self.list_commits(config, limit=1)
        if not result.get("success"):
            return None, result.get("message") or "Could not read the backup repository"
        commits = result.get("commits") or []
        if not commits:
            return None, f"Branch '{config.branch}' has no commits to restore from"
        return commits[0]["sha"], ""

    def _category_paths(self, category: RestoreCategory, available: list[str]) -> list[str]:
        """Return the paths in ``available`` that belong to ``category``."""
        if category == RestoreCategory.SETTINGS:
            return [p for p in (SETTINGS_PATH,) if p in available]
        if category == RestoreCategory.SPOOLS:
            return [p for p in (SPOOLS_PATH, SPOOL_USAGE_PATH) if p in available]
        if category == RestoreCategory.ARCHIVES:
            return [p for p in (ARCHIVES_PATH,) if p in available]
        if category == RestoreCategory.KPROFILES:
            return sorted(p for p in available if _KPROFILE_PATH_RE.match(p))
        return []

    @staticmethod
    def _parse_json_files(raw: dict[str, str]) -> tuple[dict[str, object], list[str]]:
        """Parse each fetched file, collecting paths that failed to parse."""
        parsed: dict[str, object] = {}
        bad: list[str] = []
        for path, text in raw.items():
            try:
                parsed[path] = json.loads(text)
            except (ValueError, TypeError):
                bad.append(path)
        return parsed, bad

    async def preview(self, config: GitHubBackupConfig, ref: str = "HEAD") -> dict:
        """Report which categories a commit contains, and how much is in each."""
        resolved, error = await self._resolve_ref(config, ref)
        if resolved is None:
            return {"success": False, "message": error, "ref": ref, "categories": []}

        backend = get_provider_backend(config.provider)
        client = await self._get_client()

        tree = await backend.list_tree(
            repo_url=config.repository_url, token=config.access_token, ref=resolved, client=client
        )
        if not tree.get("success"):
            return {"success": False, "message": tree.get("message") or "Could not list the commit", "ref": resolved}
        available: list[str] = tree.get("paths") or []

        # One batched read covers metadata plus every category payload.
        wanted = [METADATA_PATH] if METADATA_PATH in available else []
        for category in RestoreCategory:
            wanted.extend(self._category_paths(category, available))

        fetched = await backend.fetch_files(
            repo_url=config.repository_url, token=config.access_token, ref=resolved, paths=wanted, client=client
        )
        if not fetched.get("success"):
            return {
                "success": False,
                "message": fetched.get("message") or "Could not read the commit contents",
                "ref": resolved,
            }
        parsed, bad_paths = self._parse_json_files(fetched.get("files") or {})

        metadata = parsed.get(METADATA_PATH)
        metadata_version = metadata.get("version") if isinstance(metadata, dict) else None

        categories = []
        for category in RestoreCategory:
            paths = self._category_paths(category, available)
            if not paths:
                categories.append(
                    {
                        "category": category,
                        "available": False,
                        "item_count": 0,
                        "detail": "Not present in this backup commit",
                    }
                )
                continue
            unreadable = [p for p in paths if p in bad_paths]
            if unreadable:
                categories.append(
                    {
                        "category": category,
                        "available": False,
                        "item_count": 0,
                        "detail": f"Unreadable JSON: {', '.join(unreadable)}",
                    }
                )
                continue
            count, detail = self._count_items(category, parsed)
            categories.append({"category": category, "available": True, "item_count": count, "detail": detail})

        commit_info = None
        commits = (await self.list_commits(config, limit=20)).get("commits") or []
        for entry in commits:
            if entry["sha"] == resolved:
                commit_info = entry
                break

        return {
            "success": True,
            "message": "OK",
            "ref": resolved,
            "commit": commit_info,
            "metadata_version": metadata_version,
            "categories": categories,
        }

    @staticmethod
    def _count_items(category: RestoreCategory, parsed: dict) -> tuple[int, str | None]:
        """Count restorable items for ``category`` and describe any caveat."""
        if category == RestoreCategory.SETTINGS:
            payload = parsed.get(SETTINGS_PATH)
            values = payload.get("settings") if isinstance(payload, dict) else None
            if not isinstance(values, dict):
                return 0, "No settings in payload"
            # Both refusals are counted the same way here so the preview's item
            # count matches what the restore actually writes; the wording only
            # calls out the credential ones, which are what a user might expect
            # to come back.
            blocked = sum(1 for key in values if _is_blocked_setting_key(key))
            skipped = sum(1 for key in values if _is_skipped_setting_key(key))
            detail = f"{blocked} credential-like keys will be skipped" if blocked else None
            return len(values) - skipped, detail

        if category == RestoreCategory.SPOOLS:
            payload = parsed.get(SPOOLS_PATH)
            spools = payload.get("spools") if isinstance(payload, dict) else None
            usage_payload = parsed.get(SPOOL_USAGE_PATH)
            usage = usage_payload.get("usage_history") if isinstance(usage_payload, dict) else None
            count = len(spools) if isinstance(spools, list) else 0
            detail = f"plus {len(usage)} usage records" if isinstance(usage, list) and usage else None
            return count, detail

        if category == RestoreCategory.ARCHIVES:
            payload = parsed.get(ARCHIVES_PATH)
            archives = payload.get("archives") if isinstance(payload, dict) else None
            count = len(archives) if isinstance(archives, list) else 0
            return count, "Metadata only — 3MF files and thumbnails are not in a Git backup"

        if category == RestoreCategory.KPROFILES:
            total = 0
            serials = set()
            for path, payload in parsed.items():
                match = _KPROFILE_PATH_RE.match(path)
                if not match or not isinstance(payload, dict):
                    continue
                serials.add(match.group(1))
                profiles = payload.get("profiles")
                if isinstance(profiles, list):
                    total += len(profiles)
            detail = f"across {len(serials)} printer(s)" if serials else None
            return total, detail

        return 0, None

    # --- Restore -----------------------------------------------------------

    async def run_restore(
        self,
        config_id: int,
        ref: str,
        categories: list[RestoreCategory],
        overwrite_existing: bool = False,
    ) -> dict:
        """Apply selected categories from one backup commit."""
        # Import locally to avoid a module-level cycle: the backup service takes
        # the mirror-image lock against us.
        from backend.app.services.github_backup import github_backup_service

        # The lock serialises two concurrent restores; the backup side has no
        # lock of its own, and relies on this region staying await-free after the
        # acquisition. Both flags are plain bools on one event loop, so with no
        # suspension point between the two reads and the write, the loop cannot
        # slip github_backup.run_backup's mirror-image check in between. Adding an
        # `await` below the acquisition and above `self._running_restore = True`
        # would let a backup and a restore run at once.
        async with self._lock:
            if self._running_restore:
                return {"success": False, "message": "A restore is already running", "results": {}}
            if github_backup_service.is_running:
                return {
                    "success": False,
                    "message": "A backup is currently running. Wait for it to finish before restoring.",
                    "results": {},
                }
            self._running_restore = True

        log_id = None
        try:
            async with async_session() as db:
                result = await db.execute(select(GitHubBackupConfig).where(GitHubBackupConfig.id == config_id))
                config = result.scalar_one_or_none()
                if not config:
                    return {"success": False, "message": "Configuration not found", "results": {}}

                self._progress = "Resolving commit..."
                resolved, error = await self._resolve_ref(config, ref)
                if resolved is None:
                    return {"success": False, "message": error, "results": {}}

                log = GitHubBackupLog(config_id=config_id, status="running", trigger="restore", commit_sha=resolved)
                db.add(log)
                await db.commit()
                await db.refresh(log)
                log_id = log.id

                try:
                    payload, error = await self._read_categories(config, resolved, categories)
                    if error:
                        raise RuntimeError(error)

                    results = await self._apply(db, payload, categories, overwrite_existing)
                    await db.commit()

                    total_restored = sum(tally.restored for tally in results.values())
                    any_failed = any(tally.failed for tally in results.values())

                    log.status = "failed" if any_failed and total_restored == 0 else "success"
                    log.completed_at = datetime.now(timezone.utc)
                    log.files_changed = total_restored
                    if any_failed:
                        log.error_message = "Some items could not be restored — see the restore result for detail"
                    await db.commit()

                    return {
                        "success": True,
                        "message": f"Restored {total_restored} item(s) from {resolved[:7]}",
                        "log_id": log_id,
                        "ref": resolved,
                        "results": {name: tally.as_dict() for name, tally in results.items()},
                    }

                except Exception as e:
                    logger.exception("Restore failed for config %s ref %s", config_id, resolved)
                    await db.rollback()
                    log.status = "failed"
                    log.completed_at = datetime.now(timezone.utc)
                    log.error_message = str(e)[:1000]
                    await db.commit()
                    return {"success": False, "message": str(e), "log_id": log_id, "ref": resolved, "results": {}}

        finally:
            self._running_restore = False
            self._progress = None

    async def _read_categories(
        self, config: GitHubBackupConfig, ref: str, categories: list[RestoreCategory]
    ) -> tuple[dict, str]:
        """Fetch and parse just the files the requested categories need."""
        backend = get_provider_backend(config.provider)
        client = await self._get_client()

        self._progress = "Listing backup contents..."
        tree = await backend.list_tree(
            repo_url=config.repository_url, token=config.access_token, ref=ref, client=client
        )
        if not tree.get("success"):
            return {}, tree.get("message") or "Could not list the commit"
        available: list[str] = tree.get("paths") or []

        wanted: list[str] = []
        for category in categories:
            wanted.extend(self._category_paths(category, available))
        if not wanted:
            return {}, "None of the selected categories are present in that commit"

        self._progress = "Downloading backup files..."
        fetched = await backend.fetch_files(
            repo_url=config.repository_url, token=config.access_token, ref=ref, paths=wanted, client=client
        )
        if not fetched.get("success"):
            return {}, fetched.get("message") or "Could not read the commit contents"

        parsed, bad = self._parse_json_files(fetched.get("files") or {})
        if bad:
            return {}, f"Backup contains unreadable JSON: {', '.join(sorted(bad))}"
        return parsed, ""

    async def _apply(
        self,
        db: AsyncSession,
        payload: dict,
        categories: list[RestoreCategory],
        overwrite: bool,
    ) -> dict[str, _CategoryTally]:
        """Apply categories in dependency order and return per-category tallies."""
        results: dict[str, _CategoryTally] = {}
        archive_id_map: dict[int, int] = {}

        # Archives first: spool usage history references archive_id.
        if RestoreCategory.ARCHIVES in categories:
            self._progress = "Restoring print archives..."
            tally = _CategoryTally()
            await self._restore_archives(db, payload.get(ARCHIVES_PATH), overwrite, tally, archive_id_map)
            results[RestoreCategory.ARCHIVES.value] = tally

        if RestoreCategory.SPOOLS in categories:
            self._progress = "Restoring spool inventory..."
            tally = _CategoryTally()
            await self._restore_spools(
                db,
                payload.get(SPOOLS_PATH),
                payload.get(SPOOL_USAGE_PATH),
                overwrite,
                tally,
                archive_id_map,
            )
            results[RestoreCategory.SPOOLS.value] = tally

        if RestoreCategory.SETTINGS in categories:
            self._progress = "Restoring app settings..."
            tally = _CategoryTally()
            await self._restore_settings(db, payload.get(SETTINGS_PATH), overwrite, tally)
            results[RestoreCategory.SETTINGS.value] = tally

        # Last, because it leaves the database and publishes over MQTT.
        if RestoreCategory.KPROFILES in categories:
            self._progress = "Sending K-profiles to printers..."
            tally = _CategoryTally()
            await self._restore_kprofiles(db, payload, tally)
            results[RestoreCategory.KPROFILES.value] = tally

        return results

    # --- Per-category appliers --------------------------------------------

    async def _restore_archives(
        self,
        db: AsyncSession,
        payload,
        overwrite: bool,
        tally: _CategoryTally,
        id_map: dict[int, int],
    ) -> None:
        archives = payload.get("archives") if isinstance(payload, dict) else None
        if not isinstance(archives, list):
            tally.note("No archive data in this backup")
            return

        valid_printers = set((await db.execute(select(Printer.id))).scalars().all())
        valid_projects = set((await db.execute(select(Project.id))).scalars().all())

        # Only metadata is backed up, never the 3MF/thumbnail bytes, and
        # print_archives.file_path is NOT NULL — so inserted rows get an empty
        # path and are history-only. Say so once rather than per row.
        warned_files = False

        for entry in archives:
            if not isinstance(entry, dict):
                tally.failed += 1
                continue

            old_id = entry.get("id") if isinstance(entry.get("id"), int) else None
            started_at = _parse_dt(entry.get("started_at"))
            existing = await self._find_archive(db, entry, started_at)

            fields = {
                "print_name": entry.get("print_name"),
                "print_time_seconds": entry.get("print_time_seconds"),
                "filament_used_grams": entry.get("filament_used_grams"),
                "filament_type": entry.get("filament_type"),
                "filament_color": entry.get("filament_color"),
                "layer_height": entry.get("layer_height"),
                "total_layers": entry.get("total_layers"),
                "nozzle_diameter": entry.get("nozzle_diameter"),
                "bed_temperature": entry.get("bed_temperature"),
                "nozzle_temperature": entry.get("nozzle_temperature"),
                "sliced_for_model": entry.get("sliced_for_model"),
                "status": entry.get("status") or "completed",
                "started_at": started_at,
                "completed_at": _parse_dt(entry.get("completed_at")),
                "makerworld_url": entry.get("makerworld_url"),
                "designer": entry.get("designer"),
                "external_url": entry.get("external_url"),
                "is_favorite": bool(entry.get("is_favorite")),
                "tags": entry.get("tags"),
                "notes": entry.get("notes"),
                "cost": entry.get("cost"),
                "failure_reason": entry.get("failure_reason"),
                "quantity": entry.get("quantity") or 1,
                "energy_kwh": entry.get("energy_kwh"),
                "energy_cost": entry.get("energy_cost"),
                # A soft-deleted archive is still in the backup (its row is kept
                # so stats keep counting it), so carry the flag across or the
                # restore turns something the user deleted back into a visible
                # archive. Backups written before this key existed have no
                # deleted_at, and those rows can only come back live.
                "deleted_at": _parse_dt(entry.get("deleted_at")),
            }

            printer_id = entry.get("printer_id")
            if printer_id is not None and printer_id not in valid_printers:
                tally.note("Some archives referenced printers that no longer exist — link cleared")
                printer_id = None
            project_id = entry.get("project_id")
            if project_id is not None and project_id not in valid_projects:
                tally.note("Some archives referenced projects that no longer exist — link cleared")
                project_id = None
            fields["printer_id"] = printer_id
            fields["project_id"] = project_id

            if existing is not None:
                if old_id is not None:
                    id_map[old_id] = existing.id
                if not overwrite:
                    tally.skipped += 1
                    continue
                # Overwrite means "make the local row match the backup", which
                # includes un-deleting one the user deleted after the backup was
                # taken. Legitimate, but not obvious from a restored/skipped
                # count, so say it.
                if existing.deleted_at is not None and fields["deleted_at"] is None:
                    tally.note("Archive(s) deleted since the backup are visible again — overwrite was on")
                for key, value in fields.items():
                    setattr(existing, key, value)
                tally.restored += 1
                continue

            if not warned_files:
                tally.note(
                    "Restored archives carry metadata only — the 3MF and thumbnail files are not in a Git backup"
                )
                warned_files = True

            row = PrintArchive(
                filename=entry.get("filename") or "restored-from-backup",
                file_path="",
                file_size=entry.get("file_size") or 0,
                content_hash=entry.get("content_hash"),
                **fields,
            )
            created_at = _parse_dt(entry.get("created_at"))
            if created_at is not None:
                row.created_at = created_at
            db.add(row)
            await db.flush()
            if old_id is not None:
                id_map[old_id] = row.id
            tally.restored += 1

    async def _find_archive(self, db: AsyncSession, entry: dict, started_at: datetime | None) -> PrintArchive | None:
        """Match a backed-up archive to a local row by natural key.

        ``started_at`` is nullable and genuinely NULL for a whole class of rows —
        the re-slice path in ``library.py`` constructs ``PrintArchive`` without
        one — so it cannot be *required* by the key. It narrows the match instead:
        a backed-up row with no ``started_at`` matches a local row that has none
        either. Requiring it meant those archives never matched, so each restore
        re-inserted them as duplicates and overwrite mode could never update them.

        ``content_hash`` identifies the sliced file on its own, which is why it is
        the branch allowed to run without a ``started_at``; ``filename`` is too
        weak for that (re-slices share it) and still requires one. Two backed-up
        rows sharing a hash *and* having no ``started_at`` are indistinguishable
        in the backup, so they collapse onto one local row — better than
        duplicating both on every restore.

        Soft-deleted rows are matched deliberately: there is no ``deleted_at``
        filter here because the row still exists, and matching it is what stops a
        restore inserting a live duplicate of an archive the user has deleted.
        """
        started_predicate = PrintArchive.started_at == started_at if started_at else PrintArchive.started_at.is_(None)

        content_hash = entry.get("content_hash")
        if content_hash:
            result = await db.execute(
                select(PrintArchive).where(PrintArchive.content_hash == content_hash, started_predicate)
            )
            row = result.scalars().first()
            if row is not None:
                return row

        filename = entry.get("filename")
        if filename and started_at:
            result = await db.execute(select(PrintArchive).where(PrintArchive.filename == filename, started_predicate))
            return result.scalars().first()
        return None

    async def _restore_spools(
        self,
        db: AsyncSession,
        inventory,
        usage_payload,
        overwrite: bool,
        tally: _CategoryTally,
        archive_id_map: dict[int, int],
    ) -> None:
        spools = inventory.get("spools") if isinstance(inventory, dict) else None
        if not isinstance(spools, list):
            tally.note("No spool data in this backup")
            return

        spool_id_map: dict[int, int] = {}

        for entry in spools:
            if not isinstance(entry, dict):
                tally.failed += 1
                continue

            old_id = entry.get("id") if isinstance(entry.get("id"), int) else None
            existing = await self._find_spool(db, entry)

            fields = {
                "material": entry.get("material") or "PLA",
                "subtype": entry.get("subtype"),
                "color_name": entry.get("color_name"),
                "rgba": entry.get("rgba"),
                "brand": entry.get("brand"),
                "label_weight": entry.get("label_weight") or 1000,
                "core_weight": entry.get("core_weight") or 250,
                "weight_used": entry.get("weight_used") or 0,
                "weight_locked": bool(entry.get("weight_locked")),
                "slicer_filament": entry.get("slicer_filament"),
                "slicer_filament_name": entry.get("slicer_filament_name"),
                "nozzle_temp_min": entry.get("nozzle_temp_min"),
                "nozzle_temp_max": entry.get("nozzle_temp_max"),
                "note": entry.get("note"),
                "cost_per_kg": entry.get("cost_per_kg"),
                "tag_uid": entry.get("tag_uid"),
                "tray_uuid": entry.get("tray_uuid"),
                "data_origin": entry.get("data_origin"),
                "tag_type": entry.get("tag_type"),
                "archived_at": _parse_dt(entry.get("archived_at")),
            }

            if existing is not None:
                if old_id is not None:
                    spool_id_map[old_id] = existing.id
                if not overwrite:
                    tally.skipped += 1
                    continue
                for key, value in fields.items():
                    setattr(existing, key, value)
                tally.restored += 1
                continue

            row = Spool(**fields)
            # Carry the original created_at across. Without it the row would be
            # stamped "now", and the composite fallback in _find_spool (which
            # keys on created_at) would miss on a second restore and insert a
            # duplicate instead of matching.
            created_at = _parse_dt(entry.get("created_at"))
            if created_at is not None:
                row.created_at = created_at
            db.add(row)
            await db.flush()
            if old_id is not None:
                spool_id_map[old_id] = row.id
            tally.restored += 1

        await self._restore_spool_usage(db, usage_payload, tally, spool_id_map, archive_id_map)

    async def _find_spool(self, db: AsyncSession, entry: dict) -> Spool | None:
        """Match a backed-up spool to a local row.

        Physical identity first (an RFID/Bambu tag is the spool), then a
        descriptive composite including ``created_at`` so two otherwise
        identical spools added at different times stay distinct.
        """
        tag_uid = entry.get("tag_uid")
        if tag_uid:
            result = await db.execute(select(Spool).where(Spool.tag_uid == tag_uid))
            row = result.scalars().first()
            if row is not None:
                return row

        tray_uuid = entry.get("tray_uuid")
        if tray_uuid:
            result = await db.execute(select(Spool).where(Spool.tray_uuid == tray_uuid))
            row = result.scalars().first()
            if row is not None:
                return row

        created_at = _parse_dt(entry.get("created_at"))
        if created_at is None:
            return None
        result = await db.execute(
            select(Spool).where(
                Spool.created_at == created_at,
                Spool.material == (entry.get("material") or "PLA"),
                Spool.brand == entry.get("brand"),
                Spool.subtype == entry.get("subtype"),
                Spool.color_name == entry.get("color_name"),
            )
        )
        return result.scalars().first()

    async def _restore_spool_usage(
        self,
        db: AsyncSession,
        usage_payload,
        tally: _CategoryTally,
        spool_id_map: dict[int, int],
        archive_id_map: dict[int, int],
    ) -> None:
        usage = usage_payload.get("usage_history") if isinstance(usage_payload, dict) else None
        if not isinstance(usage, list) or not usage:
            return

        valid_printers = set((await db.execute(select(Printer.id))).scalars().all())
        unresolved = 0

        for entry in usage:
            if not isinstance(entry, dict):
                tally.failed += 1
                continue

            old_spool_id = entry.get("spool_id")
            spool_id = spool_id_map.get(old_spool_id) if isinstance(old_spool_id, int) else None
            if spool_id is None:
                # The parent spool never made it into the map: the backup's spool
                # list didn't include it, or its entry carried no integer id. A
                # spool that was merely *skipped* (matched locally, overwrite off)
                # is mapped a few lines up in _restore_spools, so it never lands
                # here — which is why the note below offers no remedy.
                unresolved += 1
                tally.skipped += 1
                continue

            created_at = _parse_dt(entry.get("created_at"))
            # Usage history has no natural key of its own, so dedupe on the
            # tuple that makes a consumption event unique in practice.
            existing = await db.execute(
                select(SpoolUsageHistory).where(
                    SpoolUsageHistory.spool_id == spool_id,
                    SpoolUsageHistory.created_at == created_at,
                    SpoolUsageHistory.weight_used == (entry.get("weight_used") or 0),
                    SpoolUsageHistory.print_name == entry.get("print_name"),
                )
            )
            if existing.scalars().first() is not None:
                tally.skipped += 1
                continue

            printer_id = entry.get("printer_id")
            if printer_id is not None and printer_id not in valid_printers:
                printer_id = None

            old_archive_id = entry.get("archive_id")
            archive_id = archive_id_map.get(old_archive_id) if isinstance(old_archive_id, int) else None

            row = SpoolUsageHistory(
                spool_id=spool_id,
                printer_id=printer_id,
                print_name=entry.get("print_name"),
                archive_id=archive_id,
                weight_used=entry.get("weight_used") or 0,
                percent_used=entry.get("percent_used") or 0,
                status=entry.get("status") or "completed",
                cost=entry.get("cost"),
            )
            if created_at is not None:
                row.created_at = created_at
            db.add(row)
            tally.restored += 1

        if unresolved:
            tally.note(
                f"{unresolved} usage record(s) skipped — their spool is not in this backup's "
                "spool list, so there is nothing to attach them to."
            )

    async def _restore_settings(self, db: AsyncSession, payload, overwrite: bool, tally: _CategoryTally) -> None:
        values = payload.get("settings") if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            tally.note("No settings data in this backup")
            return

        blocked = 0
        protected = 0
        for key, value in values.items():
            if not isinstance(key, str) or not key:
                tally.failed += 1
                continue
            if _is_blocked_setting_key(key):
                blocked += 1
                tally.skipped += 1
                continue
            if _is_protected_setting_key(key):
                protected += 1
                tally.skipped += 1
                continue
            if value is None:
                tally.skipped += 1
                continue

            result = await db.execute(select(Settings).where(Settings.key == key))
            existing = result.scalar_one_or_none()
            if existing is not None:
                if not overwrite:
                    tally.skipped += 1
                    continue
                existing.value = str(value)
                tally.restored += 1
                continue

            db.add(Settings(key=key, value=str(value)))
            tally.restored += 1

        if blocked:
            tally.note(f"{blocked} credential-like key(s) skipped — re-enter secrets manually")
        if protected:
            tally.note(
                f"{protected} authentication setting(s) skipped — change those in Settings > "
                "Authentication so the lockout checks still run"
            )

    async def _restore_kprofiles(self, db: AsyncSession, payload: dict, tally: _CategoryTally) -> None:
        by_serial: dict[str, list[tuple[str, dict]]] = {}
        for path, content in payload.items():
            match = _KPROFILE_PATH_RE.match(path)
            if not match or not isinstance(content, dict):
                continue
            by_serial.setdefault(match.group(1), []).append((match.group(2), content))

        if not by_serial:
            tally.note("No K-profile data in this backup")
            return

        result = await db.execute(select(Printer))
        printers = {p.serial_number: p for p in result.scalars().all() if p.serial_number}

        # Overwrite is not offered for K-profiles: extrusion_cali_set replaces
        # the profile occupying a slot, so writing is always an overwrite on the
        # printer side.
        tally.note("K-profiles always overwrite the matching slot on the printer")
        tally.note("The printer's acknowledgement is not reliable — verify the profiles on the printer")

        for serial, entries in sorted(by_serial.items()):
            profile_total = sum(len(c.get("profiles") or []) for _, c in entries)

            printer = printers.get(serial)
            if printer is None:
                tally.skipped += profile_total
                tally.note(f"No printer with serial {serial} — skipped")
                continue

            client = printer_manager.get_client(printer.id)
            if not client or not client.state.connected:
                tally.skipped += profile_total
                tally.note(f"{printer.name} ({serial}) is not connected — skipped")
                continue

            for nozzle, content in sorted(entries):
                profiles = content.get("profiles")
                if not isinstance(profiles, list) or not profiles:
                    continue
                if nozzle not in _KNOWN_NOZZLES:
                    tally.note(f"Unexpected nozzle diameter {nozzle} for {serial} — sent as-is")

                # The backup's slot_id is a cali_idx, and cali_idx is as
                # unstable as the autoincrement ids we already refuse to reuse
                # for spools and archives: editing a profile in Bambuddy is a
                # delete-then-add on a single-nozzle printer, which re-keys it.
                # Addressing extrusion_cali_set at a slot that no longer exists
                # is a silent no-op — the printer drops it and we would still
                # report the profile restored. So resolve the live index first.
                current = await self._current_kprofile_index(client, nozzle, serial)

                profile_dicts = []
                unmatched = 0
                for p in profiles:
                    if not isinstance(p, dict):
                        continue
                    match = self._match_kprofile(p, current)
                    if match is None:
                        unmatched += 1
                    profile_dicts.append(
                        {
                            "filament_id": p.get("filament_id", ""),
                            "name": p.get("name", ""),
                            "k_value": p.get("k_value", "0.020000"),
                            "nozzle_id": p.get("nozzle_id"),
                            "extruder_id": p.get("extruder_id", 0),
                            # Prefer the live setting_id when we matched: it is
                            # what the printer currently associates with the slot.
                            "setting_id": (match.setting_id if match else None) or p.get("setting_id"),
                            # cali_idx -1 tells the printer to add a new profile
                            # rather than address a slot that isn't there.
                            "cali_idx": match.slot_id if match else -1,
                            # Only consulted for the generated-setting_id
                            # fallback; cali_idx above takes precedence.
                            "slot_id": 0,
                        }
                    )
                if not profile_dicts:
                    continue
                if unmatched:
                    tally.note(
                        f"{unmatched} profile(s) for {nozzle} had no counterpart on {printer.name} "
                        "— added as new profiles"
                    )

                try:
                    sent = client.set_kprofiles_batch(profile_dicts, nozzle)
                except Exception as e:
                    logger.warning("K-profile restore failed for %s nozzle %s: %s", serial, nozzle, e)
                    sent = False

                if sent:
                    tally.restored += len(profile_dicts)
                else:
                    tally.failed += len(profile_dicts)
                    tally.note(f"Failed to send {nozzle} profiles to {printer.name} ({serial})")

    @staticmethod
    async def _current_kprofile_index(client, nozzle: str, serial: str) -> list:
        """Read the printer's live profiles for one nozzle.

        Best-effort: a read failure degrades to "nothing matched", which makes
        every profile an add rather than aborting the restore.
        """
        try:
            return list(await client.get_kprofiles(nozzle_diameter=nozzle) or [])
        except Exception as e:
            logger.warning("Could not read live K-profiles for %s nozzle %s: %s", serial, nozzle, e)
            return []

    @staticmethod
    def _match_kprofile(entry: dict, current: list):
        """Find the live profile a backed-up entry corresponds to.

        ``setting_id`` is the filament preset the profile was calibrated for and
        is the strongest signal; a delete-then-add edit regenerates it, so fall
        back to the display name, which Bambuddy's own editor preserves.
        Both are scoped by ``filament_id`` — the same preset on a different
        filament is a different profile.
        """
        filament_id = entry.get("filament_id")
        if not filament_id:
            return None

        candidates = [c for c in current if c.filament_id == filament_id]
        if not candidates:
            return None

        setting_id = entry.get("setting_id")
        if setting_id:
            for c in candidates:
                if c.setting_id == setting_id:
                    return c

        name = entry.get("name")
        if name:
            for c in candidates:
                if c.name == name:
                    return c

        # Exactly one profile for this filament and no better discriminator:
        # treat it as the same profile rather than duplicating it.
        return candidates[0] if len(candidates) == 1 else None


# Singleton instance
github_restore_service = GitHubRestoreService()
