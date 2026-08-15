"""Unit tests for the Git backup restore service (#2656).

Focus is on the per-category appliers: natural-key matching, the deliberate
refusal to reuse the backup's primary keys, old_id -> new_id remapping for
dependent rows, overwrite-vs-skip, the settings credential blocklist, and the
K-profile paths that depend on live printers.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.schemas.github_backup import GitHubRestoreRequest, RestoreCategory
from backend.app.services.github_restore import (
    ARCHIVES_PATH,
    SETTINGS_PATH,
    SPOOL_USAGE_PATH,
    SPOOLS_PATH,
    GitHubRestoreService,
    _CategoryTally,
    _is_blocked_setting_key,
    _is_protected_setting_key,
    _is_skipped_setting_key,
    _parse_dt,
)


def _service() -> GitHubRestoreService:
    return GitHubRestoreService()


class TestParseDt:
    def test_parses_str_datetime_the_backup_writes(self):
        assert _parse_dt("2026-07-27 06:02:05.123456") == datetime(2026, 7, 27, 6, 2, 5, 123456)

    def test_parses_iso_with_t_separator(self):
        assert _parse_dt("2026-07-27T06:02:05") == datetime(2026, 7, 27, 6, 2, 5)

    @pytest.mark.parametrize("value", ["", None, "not a date", 12345, {}])
    def test_returns_none_for_junk(self, value):
        assert _parse_dt(value) is None


class TestSettingKeyBlocklist:
    @pytest.mark.parametrize(
        "key",
        [
            "bambu_cloud_token",
            "auth_secret_key",
            "ha_token",
            "prometheus_token",
            "printer_access_code",
            "smtp_password",
            "some_api_key",
            "ftp_passphrase",
            "MQTT_SECRET",
        ],
    )
    def test_credential_like_keys_are_blocked(self, key):
        assert _is_blocked_setting_key(key) is True

    @pytest.mark.parametrize(
        "key",
        ["low_stock_threshold", "currency", "theme", "local_backup_enabled", "timezone"],
    )
    def test_ordinary_keys_are_allowed(self, key):
        assert _is_blocked_setting_key(key) is False

    @pytest.mark.parametrize(
        "key",
        ["auth_enabled", "advanced_auth_enabled", "local_login_enabled", "setup_completed"],
    )
    def test_auth_policy_keys_are_protected(self, key):
        # Not credential-shaped, so the secret hints never catch them.
        assert _is_blocked_setting_key(key) is False
        assert _is_protected_setting_key(key) is True
        assert _is_skipped_setting_key(key) is True

    @pytest.mark.parametrize("key", ["currency", "ldap_enabled", "auth_secret_key"])
    def test_protected_set_is_only_the_auth_policy_keys(self, key):
        assert _is_protected_setting_key(key) is False


class TestCategoryTally:
    def test_notes_are_deduplicated(self):
        tally = _CategoryTally()
        tally.note("same")
        tally.note("same")
        assert tally.notes == ["same"]

    def test_notes_are_bounded(self):
        tally = _CategoryTally()
        for i in range(50):
            tally.note(f"note {i}")
        assert len(tally.notes) == 20


class TestRestoreRequestSchema:
    def test_rejects_empty_category_list(self):
        with pytest.raises(ValueError):
            GitHubRestoreRequest(categories=[])

    def test_deduplicates_categories(self):
        request = GitHubRestoreRequest(
            categories=[RestoreCategory.SPOOLS, RestoreCategory.SPOOLS, RestoreCategory.SETTINGS]
        )
        assert request.categories == [RestoreCategory.SPOOLS, RestoreCategory.SETTINGS]

    def test_defaults_to_head(self):
        assert GitHubRestoreRequest(categories=[RestoreCategory.SPOOLS]).ref == "HEAD"

    @pytest.mark.parametrize("ref", ["HEAD", "abc1234", "a" * 40])
    def test_accepts_valid_refs(self, ref):
        assert GitHubRestoreRequest(ref=ref, categories=[RestoreCategory.SPOOLS]).ref == ref

    @pytest.mark.parametrize("ref", ["abc", "main", "../etc/passwd", "a" * 41, "zzzzzzz", "abc 123"])
    def test_rejects_refs_that_are_not_object_names(self, ref):
        with pytest.raises(ValueError):
            GitHubRestoreRequest(ref=ref, categories=[RestoreCategory.SPOOLS])


class TestRestoreSettings:
    @pytest.mark.asyncio
    async def test_inserts_missing_keys(self, db_session):
        tally = _CategoryTally()
        payload = {"version": "1.0", "settings": {"currency": "EUR", "theme": "dark"}}

        await _service()._restore_settings(db_session, payload, overwrite=False, tally=tally)
        await db_session.commit()

        rows = {s.key: s.value for s in (await db_session.execute(select(Settings))).scalars().all()}
        assert rows == {"currency": "EUR", "theme": "dark"}
        assert tally.restored == 2

    @pytest.mark.asyncio
    async def test_skips_existing_key_when_overwrite_off(self, db_session):
        db_session.add(Settings(key="currency", value="USD"))
        await db_session.commit()
        tally = _CategoryTally()

        await _service()._restore_settings(db_session, {"settings": {"currency": "EUR"}}, overwrite=False, tally=tally)
        await db_session.commit()

        row = (await db_session.execute(select(Settings).where(Settings.key == "currency"))).scalar_one()
        assert row.value == "USD"
        assert tally.skipped == 1
        assert tally.restored == 0

    @pytest.mark.asyncio
    async def test_overwrites_existing_key_when_enabled(self, db_session):
        db_session.add(Settings(key="currency", value="USD"))
        await db_session.commit()
        tally = _CategoryTally()

        await _service()._restore_settings(db_session, {"settings": {"currency": "EUR"}}, overwrite=True, tally=tally)
        await db_session.commit()

        row = (await db_session.execute(select(Settings).where(Settings.key == "currency"))).scalar_one()
        assert row.value == "EUR"
        assert tally.restored == 1

    @pytest.mark.asyncio
    async def test_credential_keys_are_never_restored(self, db_session):
        """A backup predating the collector's denylist can still contain secrets."""
        tally = _CategoryTally()
        payload = {"settings": {"currency": "EUR", "bambu_cloud_token": "leaked", "ha_token": "leaked"}}

        await _service()._restore_settings(db_session, payload, overwrite=True, tally=tally)
        await db_session.commit()

        keys = {s.key for s in (await db_session.execute(select(Settings))).scalars().all()}
        assert keys == {"currency"}
        assert tally.skipped == 2
        assert any("credential-like" in note for note in tally.notes)

    @pytest.mark.asyncio
    async def test_auth_settings_are_never_restored(self, db_session):
        """Restoring auth_enabled=false would disable auth behind the cache's back."""
        db_session.add(Settings(key="auth_enabled", value="true"))
        db_session.add(Settings(key="local_login_enabled", value="true"))
        await db_session.commit()
        tally = _CategoryTally()
        payload = {
            "settings": {
                "currency": "EUR",
                "auth_enabled": "false",
                "advanced_auth_enabled": "false",
                "local_login_enabled": "false",
                "setup_completed": "false",
            }
        }

        await _service()._restore_settings(db_session, payload, overwrite=True, tally=tally)
        await db_session.commit()

        rows = {s.key: s.value for s in (await db_session.execute(select(Settings))).scalars().all()}
        assert rows["auth_enabled"] == "true"
        assert rows["local_login_enabled"] == "true"
        assert "advanced_auth_enabled" not in rows
        assert "setup_completed" not in rows
        assert rows["currency"] == "EUR"
        assert tally.restored == 1
        assert tally.skipped == 4
        assert any("authentication setting" in note for note in tally.notes)

    @pytest.mark.asyncio
    async def test_missing_payload_is_noted_not_fatal(self, db_session):
        tally = _CategoryTally()
        await _service()._restore_settings(db_session, None, overwrite=True, tally=tally)
        assert tally.restored == 0
        assert tally.notes


class TestRestoreSpools:
    def _spool_entry(self, **overrides):
        entry = {
            "id": 41,
            "material": "PLA",
            "subtype": "Basic",
            "color_name": "Jade White",
            "brand": "Bambu Lab",
            "tag_uid": "AABBCCDD",
            "created_at": "2026-01-05 12:00:00",
            "weight_used": 120.5,
        }
        entry.update(overrides)
        return entry

    @pytest.mark.asyncio
    async def test_inserts_without_reusing_backup_id(self, db_session):
        """The backup's spool.id belongs to an unrelated row today."""
        db_session.add(Spool(material="PETG"))  # occupies id 1
        await db_session.commit()

        tally = _CategoryTally()
        payload = {"spools": [self._spool_entry(id=1)]}

        await _service()._restore_spools(db_session, payload, None, False, tally, {})
        await db_session.commit()

        spools = (await db_session.execute(select(Spool))).scalars().all()
        assert len(spools) == 2
        restored = next(s for s in spools if s.tag_uid == "AABBCCDD")
        assert restored.id != 1
        assert restored.material == "PLA"

    @pytest.mark.asyncio
    async def test_matches_existing_spool_by_tag_uid(self, db_session):
        db_session.add(Spool(material="PLA", tag_uid="AABBCCDD", color_name="Old"))
        await db_session.commit()
        tally = _CategoryTally()

        await _service()._restore_spools(db_session, {"spools": [self._spool_entry()]}, None, False, tally, {})
        await db_session.commit()

        assert len((await db_session.execute(select(Spool))).scalars().all()) == 1
        assert tally.skipped == 1

    @pytest.mark.asyncio
    async def test_matches_existing_spool_by_tray_uuid(self, db_session):
        db_session.add(Spool(material="PLA", tray_uuid="1234" * 8))
        await db_session.commit()
        tally = _CategoryTally()
        entry = self._spool_entry(tag_uid=None, tray_uuid="1234" * 8)

        await _service()._restore_spools(db_session, {"spools": [entry]}, None, False, tally, {})
        await db_session.commit()

        assert len((await db_session.execute(select(Spool))).scalars().all()) == 1
        assert tally.skipped == 1

    @pytest.mark.asyncio
    async def test_matches_tagless_spool_by_descriptive_composite(self, db_session):
        """Manually added spools have no tag, so fall back to created_at + description."""
        db_session.add(
            Spool(
                material="PLA",
                subtype="Basic",
                color_name="Jade White",
                brand="Bambu Lab",
                created_at=datetime(2026, 1, 5, 12, 0, 0),
            )
        )
        await db_session.commit()
        tally = _CategoryTally()

        entry = self._spool_entry(tag_uid=None)
        await _service()._restore_spools(db_session, {"spools": [entry]}, None, False, tally, {})
        await db_session.commit()

        assert len((await db_session.execute(select(Spool))).scalars().all()) == 1
        assert tally.skipped == 1

    @pytest.mark.asyncio
    async def test_overwrite_updates_matched_spool(self, db_session):
        db_session.add(Spool(material="PLA", tag_uid="AABBCCDD", color_name="Old", weight_used=0))
        await db_session.commit()
        tally = _CategoryTally()

        await _service()._restore_spools(db_session, {"spools": [self._spool_entry()]}, None, True, tally, {})
        await db_session.commit()

        row = (await db_session.execute(select(Spool))).scalar_one()
        assert row.color_name == "Jade White"
        assert row.weight_used == 120.5
        assert tally.restored == 1

    @pytest.mark.asyncio
    async def test_insert_preserves_created_at_so_repeat_restore_is_idempotent(self, db_session):
        """Second restore of the same backup must match, not duplicate."""
        service = _service()
        payload = {"spools": [self._spool_entry(tag_uid=None)]}

        await service._restore_spools(db_session, payload, None, False, _CategoryTally(), {})
        await db_session.commit()
        await service._restore_spools(db_session, payload, None, False, _CategoryTally(), {})
        await db_session.commit()

        spools = (await db_session.execute(select(Spool))).scalars().all()
        assert len(spools) == 1
        assert spools[0].created_at == datetime(2026, 1, 5, 12, 0, 0)

    @pytest.mark.asyncio
    async def test_usage_history_spool_id_is_remapped(self, db_session):
        """Usage rows must point at the new local spool id, not the backup's."""
        tally = _CategoryTally()
        inventory = {"spools": [self._spool_entry(id=41)]}
        usage = {
            "usage_history": [
                {
                    "id": 900,
                    "spool_id": 41,
                    "printer_id": None,
                    "print_name": "benchy.3mf",
                    "archive_id": None,
                    "weight_used": 12.0,
                    "percent_used": 5,
                    "status": "completed",
                    "created_at": "2026-02-01 09:00:00",
                }
            ]
        }

        await _service()._restore_spools(db_session, inventory, usage, False, tally, {})
        await db_session.commit()

        spool = (await db_session.execute(select(Spool))).scalar_one()
        row = (await db_session.execute(select(SpoolUsageHistory))).scalar_one()
        assert row.spool_id == spool.id
        assert row.print_name == "benchy.3mf"

    @pytest.mark.asyncio
    async def test_usage_history_archive_id_is_remapped(self, db_session):
        tally = _CategoryTally()
        inventory = {"spools": [self._spool_entry(id=41)]}
        usage = {
            "usage_history": [
                {
                    "spool_id": 41,
                    "archive_id": 77,
                    "weight_used": 1.0,
                    "created_at": "2026-02-01 09:00:00",
                }
            ]
        }
        archive = PrintArchive(filename="a.3mf", file_path="", file_size=1)
        db_session.add(archive)
        await db_session.flush()

        await _service()._restore_spools(db_session, inventory, usage, False, tally, {77: archive.id})
        await db_session.commit()

        row = (await db_session.execute(select(SpoolUsageHistory))).scalar_one()
        assert row.archive_id == archive.id

    @pytest.mark.asyncio
    async def test_usage_row_with_unresolvable_spool_is_skipped_and_explained(self, db_session):
        tally = _CategoryTally()
        usage = {"usage_history": [{"spool_id": 999, "weight_used": 1.0, "created_at": "2026-02-01 09:00:00"}]}

        await _service()._restore_spools(db_session, {"spools": []}, usage, False, tally, {})
        await db_session.commit()

        assert (await db_session.execute(select(SpoolUsageHistory))).scalars().first() is None
        assert tally.skipped == 1
        assert any("their spool is not in this backup's spool list" in note for note in tally.notes)
        # No remedy is offered, because none exists: overwrite does not change
        # which spools land in the map (a skipped spool is mapped anyway), and
        # usage history is always restored alongside the spools category.
        assert not any("overwrite" in note.lower() for note in tally.notes)

    @pytest.mark.asyncio
    async def test_usage_resolves_against_a_spool_skipped_because_overwrite_is_off(self, db_session):
        """A skipped spool is still mapped, so its usage rows are not "unresolved".

        This is why the note above offers no remedy: turning overwrite on would
        not rescue anything, and saying so misdescribed which records are lost.
        """
        db_session.add(Spool(material="PLA", tag_uid="AABBCCDD", color_name="Old"))
        await db_session.commit()
        tally = _CategoryTally()
        inventory = {"spools": [self._spool_entry(id=41)]}
        usage = {
            "usage_history": [
                {"spool_id": 41, "print_name": "b.3mf", "weight_used": 5.0, "created_at": "2026-02-01 09:00:00"}
            ]
        }

        await _service()._restore_spools(db_session, inventory, usage, False, tally, {})
        await db_session.commit()

        spool = (await db_session.execute(select(Spool))).scalar_one()
        row = (await db_session.execute(select(SpoolUsageHistory))).scalar_one()
        assert row.spool_id == spool.id
        assert not any("spool list" in note for note in tally.notes)

    @pytest.mark.asyncio
    async def test_usage_history_is_not_duplicated_on_repeat_restore(self, db_session):
        service = _service()
        inventory = {"spools": [self._spool_entry(id=41)]}
        usage = {
            "usage_history": [
                {"spool_id": 41, "print_name": "b.3mf", "weight_used": 5.0, "created_at": "2026-02-01 09:00:00"}
            ]
        }

        await service._restore_spools(db_session, inventory, usage, False, _CategoryTally(), {})
        await db_session.commit()
        await service._restore_spools(db_session, inventory, usage, False, _CategoryTally(), {})
        await db_session.commit()

        rows = (await db_session.execute(select(SpoolUsageHistory))).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_dangling_printer_id_is_cleared(self, db_session):
        tally = _CategoryTally()
        inventory = {"spools": [self._spool_entry(id=41)]}
        usage = {
            "usage_history": [
                {"spool_id": 41, "printer_id": 4242, "weight_used": 1.0, "created_at": "2026-02-01 09:00:00"}
            ]
        }

        await _service()._restore_spools(db_session, inventory, usage, False, tally, {})
        await db_session.commit()

        row = (await db_session.execute(select(SpoolUsageHistory))).scalar_one()
        assert row.printer_id is None


class TestRestoreArchives:
    def _archive_entry(self, **overrides):
        entry = {
            "id": 77,
            "filename": "benchy.3mf",
            "file_size": 2048,
            "content_hash": "abc123",
            "print_name": "Benchy",
            "status": "completed",
            "started_at": "2026-03-01 10:00:00",
            "completed_at": "2026-03-01 11:00:00",
            "created_at": "2026-03-01 10:00:00",
            "quantity": 1,
            "is_favorite": False,
        }
        entry.update(overrides)
        return entry

    @pytest.mark.asyncio
    async def test_inserts_metadata_only_row_with_empty_file_path(self, db_session):
        """print_archives.file_path is NOT NULL but is not in the backup."""
        tally = _CategoryTally()
        id_map: dict[int, int] = {}

        await _service()._restore_archives(db_session, {"archives": [self._archive_entry()]}, False, tally, id_map)
        await db_session.commit()

        row = (await db_session.execute(select(PrintArchive))).scalar_one()
        assert row.file_path == ""
        assert row.filename == "benchy.3mf"
        assert row.id != 77
        assert id_map == {77: row.id}
        assert any("metadata only" in note for note in tally.notes)

    @pytest.mark.asyncio
    async def test_matches_existing_archive_by_hash_and_start(self, db_session):
        db_session.add(
            PrintArchive(
                filename="benchy.3mf",
                file_path="/data/benchy.3mf",
                file_size=2048,
                content_hash="abc123",
                started_at=datetime(2026, 3, 1, 10, 0, 0),
            )
        )
        await db_session.commit()
        tally = _CategoryTally()

        await _service()._restore_archives(db_session, {"archives": [self._archive_entry()]}, False, tally, {})
        await db_session.commit()

        rows = (await db_session.execute(select(PrintArchive))).scalars().all()
        assert len(rows) == 1
        assert rows[0].file_path == "/data/benchy.3mf"
        assert tally.skipped == 1

    @pytest.mark.asyncio
    async def test_falls_back_to_filename_and_start_without_hash(self, db_session):
        db_session.add(
            PrintArchive(
                filename="benchy.3mf",
                file_path="/data/benchy.3mf",
                file_size=2048,
                started_at=datetime(2026, 3, 1, 10, 0, 0),
            )
        )
        await db_session.commit()
        tally = _CategoryTally()

        entry = self._archive_entry(content_hash=None)
        await _service()._restore_archives(db_session, {"archives": [entry]}, False, tally, {})
        await db_session.commit()

        assert len((await db_session.execute(select(PrintArchive))).scalars().all()) == 1
        assert tally.skipped == 1

    @pytest.mark.asyncio
    async def test_matches_archive_with_no_started_at_by_hash(self, db_session):
        """started_at is NULL for re-sliced archives, so it cannot be required.

        Gating both match branches on it meant these rows never matched: every
        restore re-inserted them and overwrite mode could never update them.
        """
        db_session.add(
            PrintArchive(
                filename="benchy.3mf",
                file_path="/data/benchy.3mf",
                file_size=2048,
                content_hash="abc123",
                started_at=None,
            )
        )
        await db_session.commit()
        tally = _CategoryTally()

        entry = self._archive_entry(started_at=None)
        await _service()._restore_archives(db_session, {"archives": [entry]}, False, tally, {})
        await db_session.commit()

        assert len((await db_session.execute(select(PrintArchive))).scalars().all()) == 1
        assert tally.skipped == 1

    @pytest.mark.asyncio
    async def test_started_at_still_discriminates_when_present(self, db_session):
        """A NULL-tolerant match must not collapse rows that do differ."""
        db_session.add(
            PrintArchive(
                filename="benchy.3mf",
                file_path="/data/benchy.3mf",
                file_size=2048,
                content_hash="abc123",
                started_at=datetime(2026, 3, 1, 10, 0, 0),
            )
        )
        await db_session.commit()
        tally = _CategoryTally()

        # Same file, no start time recorded — a different row, not that one.
        entry = self._archive_entry(started_at=None)
        await _service()._restore_archives(db_session, {"archives": [entry]}, False, tally, {})
        await db_session.commit()

        assert len((await db_session.execute(select(PrintArchive))).scalars().all()) == 2
        assert tally.restored == 1

    @pytest.mark.asyncio
    async def test_soft_deleted_archive_is_not_restored_as_visible(self, db_session):
        """A backup keeps soft-deleted rows, so the flag has to survive.

        Their row is retained on purpose (stats keep counting the filament and
        energy), so without carrying deleted_at a restore turns an archive the
        user deleted back into a visible one.
        """
        tally = _CategoryTally()
        entry = self._archive_entry(deleted_at="2026-03-02 08:00:00")

        await _service()._restore_archives(db_session, {"archives": [entry]}, False, tally, {})
        await db_session.commit()

        row = (await db_session.execute(select(PrintArchive))).scalar_one()
        assert row.deleted_at == datetime(2026, 3, 2, 8, 0, 0)
        assert tally.restored == 1

    @pytest.mark.asyncio
    async def test_locally_deleted_archive_stays_deleted_without_overwrite(self, db_session):
        db_session.add(
            PrintArchive(
                filename="benchy.3mf",
                file_path="",
                file_size=2048,
                content_hash="abc123",
                started_at=datetime(2026, 3, 1, 10, 0, 0),
                deleted_at=datetime(2026, 3, 5, 9, 0, 0),
            )
        )
        await db_session.commit()
        tally = _CategoryTally()

        # The backup predates the deletion, so its copy is live.
        await _service()._restore_archives(db_session, {"archives": [self._archive_entry()]}, False, tally, {})
        await db_session.commit()

        row = (await db_session.execute(select(PrintArchive))).scalar_one()
        assert row.deleted_at == datetime(2026, 3, 5, 9, 0, 0)
        assert tally.skipped == 1

    @pytest.mark.asyncio
    async def test_overwrite_undeletes_a_locally_deleted_archive_and_says_so(self, db_session):
        db_session.add(
            PrintArchive(
                filename="benchy.3mf",
                file_path="",
                file_size=2048,
                content_hash="abc123",
                started_at=datetime(2026, 3, 1, 10, 0, 0),
                deleted_at=datetime(2026, 3, 5, 9, 0, 0),
            )
        )
        await db_session.commit()
        tally = _CategoryTally()

        await _service()._restore_archives(db_session, {"archives": [self._archive_entry()]}, True, tally, {})
        await db_session.commit()

        row = (await db_session.execute(select(PrintArchive))).scalar_one()
        assert row.deleted_at is None
        assert tally.restored == 1
        assert any("visible again" in note for note in tally.notes)

    @pytest.mark.asyncio
    async def test_overwrite_updates_metadata_but_keeps_local_file_path(self, db_session):
        db_session.add(
            PrintArchive(
                filename="benchy.3mf",
                file_path="/data/benchy.3mf",
                file_size=2048,
                content_hash="abc123",
                started_at=datetime(2026, 3, 1, 10, 0, 0),
                notes="old",
            )
        )
        await db_session.commit()
        tally = _CategoryTally()

        entry = self._archive_entry(notes="restored note")
        await _service()._restore_archives(db_session, {"archives": [entry]}, True, tally, {})
        await db_session.commit()

        row = (await db_session.execute(select(PrintArchive))).scalar_one()
        assert row.notes == "restored note"
        # The 3MF on disk must not be orphaned by a metadata restore.
        assert row.file_path == "/data/benchy.3mf"
        assert tally.restored == 1

    @pytest.mark.asyncio
    async def test_dangling_printer_and_project_links_are_cleared(self, db_session):
        tally = _CategoryTally()
        entry = self._archive_entry(printer_id=4242, project_id=4343)

        await _service()._restore_archives(db_session, {"archives": [entry]}, False, tally, {})
        await db_session.commit()

        row = (await db_session.execute(select(PrintArchive))).scalar_one()
        assert row.printer_id is None
        assert row.project_id is None
        assert any("no longer exist" in note for note in tally.notes)

    @pytest.mark.asyncio
    async def test_valid_printer_link_is_preserved(self, db_session, printer_factory):
        printer = await printer_factory()
        tally = _CategoryTally()
        entry = self._archive_entry(printer_id=printer.id)

        await _service()._restore_archives(db_session, {"archives": [entry]}, False, tally, {})
        await db_session.commit()

        row = (await db_session.execute(select(PrintArchive))).scalar_one()
        assert row.printer_id == printer.id

    @pytest.mark.asyncio
    async def test_non_dict_entry_counts_as_failed(self, db_session):
        tally = _CategoryTally()
        await _service()._restore_archives(db_session, {"archives": ["nonsense"]}, False, tally, {})
        assert tally.failed == 1


class TestRestoreKprofiles:
    @staticmethod
    def _live(slot_id, filament_id="GFA00", name="Bambu PLA", setting_id="PFUS123"):
        """One profile as the printer currently reports it."""
        return SimpleNamespace(slot_id=slot_id, filament_id=filament_id, name=name, setting_id=setting_id)

    def _client(self, live=None, sent=True):
        client = MagicMock()
        client.state.connected = True
        client.set_kprofiles_batch = MagicMock(return_value=sent)
        client.get_kprofiles = AsyncMock(return_value=list(live or []))
        return client

    def _payload(self, serial="00M09A123456789", nozzle="0.4"):
        return {
            f"kprofiles/{serial}/{nozzle}.json": {
                "version": "1.0",
                "printer_serial": serial,
                "nozzle_diameter": nozzle,
                "profiles": [
                    {
                        "slot_id": 0,
                        "name": "Bambu PLA",
                        "k_value": "0.020000",
                        "filament_id": "GFA00",
                        "nozzle_id": "HS00-0.4",
                        "extruder_id": 0,
                        "setting_id": "PFUS123",
                    }
                ],
            }
        }

    @pytest.mark.asyncio
    async def test_sends_batch_to_connected_printer(self, db_session, printer_factory):
        printer = await printer_factory(serial_number="00M09A123456789")
        client = MagicMock()
        client.state.connected = True
        client.set_kprofiles_batch = MagicMock(return_value=True)
        tally = _CategoryTally()

        with patch("backend.app.services.github_restore.printer_manager") as manager:
            manager.get_client = MagicMock(return_value=client)
            await _service()._restore_kprofiles(db_session, self._payload(), tally)

        client.set_kprofiles_batch.assert_called_once()
        profiles, nozzle = client.set_kprofiles_batch.call_args.args
        assert nozzle == "0.4"
        assert profiles[0]["name"] == "Bambu PLA"
        assert profiles[0]["filament_id"] == "GFA00"
        assert tally.restored == 1
        assert manager.get_client.call_args.args == (printer.id,)

    @pytest.mark.asyncio
    async def test_always_warns_to_verify_on_the_printer(self, db_session, printer_factory):
        await printer_factory(serial_number="00M09A123456789")
        client = self._client()
        tally = _CategoryTally()

        with patch("backend.app.services.github_restore.printer_manager") as manager:
            manager.get_client = MagicMock(return_value=client)
            await _service()._restore_kprofiles(db_session, self._payload(), tally)

        # The printer does answer extrusion_cali_set, but it reports "fail" on
        # writes that land, so the note must not promise either way.
        assert any("verify the profiles on the printer" in note for note in tally.notes)
        assert not any("without acknowledgement" in note for note in tally.notes)
        assert any("always overwrite" in note for note in tally.notes)

    # --- cali_idx is resolved live, never taken from the backup -------------
    #
    # Regression cover for the silent no-op found testing on an X1E: the backup
    # stored cali_idx 8151, a Bambuddy edit re-keyed the profile to 4606, and
    # the restore aimed extrusion_cali_set at 8151. The printer dropped it and
    # the tally still said "1 restored".

    @pytest.mark.asyncio
    async def test_uses_the_live_cali_idx_not_the_backed_up_slot(self, db_session, printer_factory):
        await printer_factory(serial_number="00M09A123456789")
        payload = self._payload()
        payload["kprofiles/00M09A123456789/0.4.json"]["profiles"][0]["slot_id"] = 8151
        client = self._client(live=[self._live(slot_id=4606)])
        tally = _CategoryTally()

        with patch("backend.app.services.github_restore.printer_manager") as manager:
            manager.get_client = MagicMock(return_value=client)
            await _service()._restore_kprofiles(db_session, payload, tally)

        client.get_kprofiles.assert_awaited_once_with(nozzle_diameter="0.4")
        profiles, _ = client.set_kprofiles_batch.call_args.args
        assert profiles[0]["cali_idx"] == 4606, "must address the slot that exists now"
        assert profiles[0]["cali_idx"] != 8151, "must not reuse the backup's cali_idx"
        assert tally.restored == 1

    @pytest.mark.asyncio
    async def test_matches_on_name_when_setting_id_was_regenerated(self, db_session, printer_factory):
        # A delete-then-add edit mints a fresh setting_id, so the name carries
        # the match instead.
        await printer_factory(serial_number="00M09A123456789")
        client = self._client(live=[self._live(slot_id=4606, setting_id="PF9999999999")])
        tally = _CategoryTally()

        with patch("backend.app.services.github_restore.printer_manager") as manager:
            manager.get_client = MagicMock(return_value=client)
            await _service()._restore_kprofiles(db_session, self._payload(), tally)

        profiles, _ = client.set_kprofiles_batch.call_args.args
        assert profiles[0]["cali_idx"] == 4606
        # The live setting_id wins: it is what the printer associates with the slot.
        assert profiles[0]["setting_id"] == "PF9999999999"

    @pytest.mark.asyncio
    async def test_unmatched_profile_is_added_rather_than_aimed_at_a_dead_slot(self, db_session, printer_factory):
        await printer_factory(serial_number="00M09A123456789")
        client = self._client(live=[])  # printer has nothing for this nozzle
        tally = _CategoryTally()

        with patch("backend.app.services.github_restore.printer_manager") as manager:
            manager.get_client = MagicMock(return_value=client)
            await _service()._restore_kprofiles(db_session, self._payload(), tally)

        profiles, _ = client.set_kprofiles_batch.call_args.args
        assert profiles[0]["cali_idx"] == -1, "-1 tells the printer to add a new profile"
        assert profiles[0]["setting_id"] == "PFUS123", "falls back to the backed-up preset"
        assert any("added as new profiles" in note for note in tally.notes)

    @pytest.mark.asyncio
    async def test_different_filament_is_not_treated_as_a_match(self, db_session, printer_factory):
        # Same slot, different filament — matching on slot alone would clobber
        # an unrelated profile.
        await printer_factory(serial_number="00M09A123456789")
        client = self._client(live=[self._live(slot_id=4606, filament_id="GFB99", name="Bambu PLA")])
        tally = _CategoryTally()

        with patch("backend.app.services.github_restore.printer_manager") as manager:
            manager.get_client = MagicMock(return_value=client)
            await _service()._restore_kprofiles(db_session, self._payload(), tally)

        profiles, _ = client.set_kprofiles_batch.call_args.args
        assert profiles[0]["cali_idx"] == -1

    @pytest.mark.asyncio
    async def test_unreadable_live_index_degrades_to_adding(self, db_session, printer_factory):
        # A failed read must not abort the restore.
        await printer_factory(serial_number="00M09A123456789")
        client = self._client()
        client.get_kprofiles = AsyncMock(side_effect=RuntimeError("mqtt timeout"))
        tally = _CategoryTally()

        with patch("backend.app.services.github_restore.printer_manager") as manager:
            manager.get_client = MagicMock(return_value=client)
            await _service()._restore_kprofiles(db_session, self._payload(), tally)

        profiles, _ = client.set_kprofiles_batch.call_args.args
        assert profiles[0]["cali_idx"] == -1
        assert tally.restored == 1

    @pytest.mark.asyncio
    async def test_sole_profile_for_a_filament_matches_without_setting_id_or_name(self, db_session, printer_factory):
        await printer_factory(serial_number="00M09A123456789")
        payload = self._payload()
        entry = payload["kprofiles/00M09A123456789/0.4.json"]["profiles"][0]
        entry["setting_id"] = None
        entry["name"] = ""
        client = self._client(live=[self._live(slot_id=4606, setting_id="PFOTHER", name="Renamed")])
        tally = _CategoryTally()

        with patch("backend.app.services.github_restore.printer_manager") as manager:
            manager.get_client = MagicMock(return_value=client)
            await _service()._restore_kprofiles(db_session, payload, tally)

        profiles, _ = client.set_kprofiles_batch.call_args.args
        assert profiles[0]["cali_idx"] == 4606

    @pytest.mark.asyncio
    async def test_ambiguous_filament_without_discriminator_is_added_not_guessed(self, db_session, printer_factory):
        await printer_factory(serial_number="00M09A123456789")
        payload = self._payload()
        entry = payload["kprofiles/00M09A123456789/0.4.json"]["profiles"][0]
        entry["setting_id"] = None
        entry["name"] = ""
        client = self._client(live=[self._live(slot_id=1, setting_id="A"), self._live(slot_id=2, setting_id="B")])
        tally = _CategoryTally()

        with patch("backend.app.services.github_restore.printer_manager") as manager:
            manager.get_client = MagicMock(return_value=client)
            await _service()._restore_kprofiles(db_session, payload, tally)

        profiles, _ = client.set_kprofiles_batch.call_args.args
        assert profiles[0]["cali_idx"] == -1, "two candidates and nothing to tell them apart"

    @pytest.mark.asyncio
    async def test_unknown_serial_is_skipped_with_reason(self, db_session):
        tally = _CategoryTally()

        with patch("backend.app.services.github_restore.printer_manager"):
            await _service()._restore_kprofiles(db_session, self._payload(serial="NOSUCH"), tally)

        assert tally.restored == 0
        assert tally.skipped == 1
        assert any("No printer with serial NOSUCH" in note for note in tally.notes)

    @pytest.mark.asyncio
    async def test_offline_printer_is_skipped_not_failed(self, db_session, printer_factory):
        await printer_factory(serial_number="00M09A123456789", name="Shelf Printer")
        client = MagicMock()
        client.state.connected = False
        tally = _CategoryTally()

        with patch("backend.app.services.github_restore.printer_manager") as manager:
            manager.get_client = MagicMock(return_value=client)
            await _service()._restore_kprofiles(db_session, self._payload(), tally)

        assert tally.skipped == 1
        assert tally.failed == 0
        assert any("not connected" in note for note in tally.notes)

    @pytest.mark.asyncio
    async def test_no_client_at_all_is_skipped(self, db_session, printer_factory):
        await printer_factory(serial_number="00M09A123456789")
        tally = _CategoryTally()

        with patch("backend.app.services.github_restore.printer_manager") as manager:
            manager.get_client = MagicMock(return_value=None)
            await _service()._restore_kprofiles(db_session, self._payload(), tally)

        assert tally.skipped == 1

    @pytest.mark.asyncio
    async def test_publish_failure_counts_as_failed(self, db_session, printer_factory):
        await printer_factory(serial_number="00M09A123456789")
        client = MagicMock()
        client.state.connected = True
        client.set_kprofiles_batch = MagicMock(return_value=False)
        tally = _CategoryTally()

        with patch("backend.app.services.github_restore.printer_manager") as manager:
            manager.get_client = MagicMock(return_value=client)
            await _service()._restore_kprofiles(db_session, self._payload(), tally)

        assert tally.failed == 1
        assert tally.restored == 0

    @pytest.mark.asyncio
    async def test_publish_exception_is_contained(self, db_session, printer_factory):
        await printer_factory(serial_number="00M09A123456789")
        client = MagicMock()
        client.state.connected = True
        client.set_kprofiles_batch = MagicMock(side_effect=RuntimeError("mqtt down"))
        tally = _CategoryTally()

        with patch("backend.app.services.github_restore.printer_manager") as manager:
            manager.get_client = MagicMock(return_value=client)
            await _service()._restore_kprofiles(db_session, self._payload(), tally)

        assert tally.failed == 1

    @pytest.mark.asyncio
    async def test_each_nozzle_is_sent_separately(self, db_session, printer_factory):
        await printer_factory(serial_number="00M09A123456789")
        payload = {**self._payload(nozzle="0.4"), **self._payload(nozzle="0.8")}
        client = MagicMock()
        client.state.connected = True
        client.set_kprofiles_batch = MagicMock(return_value=True)
        tally = _CategoryTally()

        with patch("backend.app.services.github_restore.printer_manager") as manager:
            manager.get_client = MagicMock(return_value=client)
            await _service()._restore_kprofiles(db_session, payload, tally)

        assert client.set_kprofiles_batch.call_count == 2
        assert {c.args[1] for c in client.set_kprofiles_batch.call_args_list} == {"0.4", "0.8"}
        assert tally.restored == 2

    @pytest.mark.asyncio
    async def test_empty_payload_is_noted(self, db_session):
        tally = _CategoryTally()
        await _service()._restore_kprofiles(db_session, {}, tally)
        assert any("No K-profile data" in note for note in tally.notes)


class TestSoftDeletedArchiveRoundTrip:
    """The two halves of the soft-delete fix only work together.

    The collector keeps soft-deleted rows on purpose (their stats still count),
    so if it doesn't write ``deleted_at`` there is nothing for the restore to
    carry across and a deleted archive comes back visible. Covered end to end
    because each half looks harmless on its own.
    """

    @pytest.mark.asyncio
    async def test_deleted_at_survives_collect_then_restore(self, db_session):
        from backend.app.services.github_backup import github_backup_service

        deleted_at = datetime(2026, 3, 5, 9, 0, 0)
        db_session.add(
            PrintArchive(
                filename="trashed.3mf",
                file_path="",
                file_size=1024,
                content_hash="hash-trashed",
                started_at=datetime(2026, 3, 1, 10, 0, 0),
                deleted_at=deleted_at,
            )
        )
        await db_session.commit()

        files: dict = {}
        await github_backup_service._collect_archives(db_session, files)
        payload = files[ARCHIVES_PATH]
        assert payload["archives"][0]["deleted_at"] == str(deleted_at)

        # Restore that payload into an instance where the row is gone entirely.
        await db_session.execute(PrintArchive.__table__.delete())
        await db_session.commit()

        tally = _CategoryTally()
        await _service()._restore_archives(db_session, payload, False, tally, {})
        await db_session.commit()

        row = (await db_session.execute(select(PrintArchive))).scalar_one()
        assert row.deleted_at == deleted_at, "a deleted archive must not come back visible"


class TestCategoryPathMapping:
    def setup_method(self):
        self.service = _service()
        self.available = [
            "backup_metadata.json",
            SETTINGS_PATH,
            SPOOLS_PATH,
            SPOOL_USAGE_PATH,
            ARCHIVES_PATH,
            "kprofiles/SERIAL1/0.4.json",
            "kprofiles/SERIAL1/0.8.json",
            "cloud_profiles/filament.json",
        ]

    def test_spools_includes_usage_history(self):
        paths = self.service._category_paths(RestoreCategory.SPOOLS, self.available)
        assert paths == [SPOOLS_PATH, SPOOL_USAGE_PATH]

    def test_kprofiles_globs_all_serials_and_nozzles(self):
        paths = self.service._category_paths(RestoreCategory.KPROFILES, self.available)
        assert paths == ["kprofiles/SERIAL1/0.4.json", "kprofiles/SERIAL1/0.8.json"]

    def test_absent_paths_are_omitted(self):
        paths = self.service._category_paths(RestoreCategory.SETTINGS, ["backup_metadata.json"])
        assert paths == []

    def test_cloud_profiles_are_not_a_restore_category(self):
        assert "cloud_profiles" not in {c.value for c in RestoreCategory}


class TestMutex:
    @pytest.mark.asyncio
    async def test_restore_refuses_while_a_backup_is_running(self):
        service = _service()
        with patch("backend.app.services.github_backup.github_backup_service") as backup:
            backup.is_running = True
            result = await service.run_restore(1, "HEAD", [RestoreCategory.SPOOLS])

        assert result["success"] is False
        assert "backup is currently running" in result["message"]

    @pytest.mark.asyncio
    async def test_restore_refuses_while_another_restore_is_running(self):
        service = _service()
        service._running_restore = True

        result = await service.run_restore(1, "HEAD", [RestoreCategory.SPOOLS])

        assert result["success"] is False
        assert "restore is already running" in result["message"]

    @pytest.mark.asyncio
    async def test_backup_refuses_while_a_restore_is_running(self):
        from backend.app.services.github_backup import GitHubBackupService

        backup_service = GitHubBackupService()
        with patch("backend.app.services.github_restore.github_restore_service") as restore:
            restore.is_running = True
            result = await backup_service.run_backup(1, trigger="manual")

        assert result["success"] is False
        assert "restore is currently running" in result["message"]


class TestResolveRef:
    @pytest.mark.asyncio
    async def test_concrete_sha_passes_through_without_an_api_call(self):
        service = _service()
        service.list_commits = AsyncMock()
        config = MagicMock(branch="main")

        resolved, error = await service._resolve_ref(config, "abc1234")

        assert resolved == "abc1234"
        assert error == ""
        service.list_commits.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_head_resolves_to_the_tip_sha(self):
        service = _service()
        service.list_commits = AsyncMock(
            return_value={"success": True, "commits": [{"sha": "tipsha1"}, {"sha": "older"}]}
        )
        config = MagicMock(branch="main")

        resolved, error = await service._resolve_ref(config, "HEAD")

        assert resolved == "tipsha1"
        assert error == ""

    @pytest.mark.asyncio
    async def test_empty_history_is_an_error(self):
        service = _service()
        service.list_commits = AsyncMock(return_value={"success": True, "commits": []})
        config = MagicMock(branch="main")

        resolved, error = await service._resolve_ref(config, "HEAD")

        assert resolved is None
        assert "no commits" in error
