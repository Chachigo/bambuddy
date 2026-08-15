"""Integration tests for the Git backup restore API endpoints (#2656)."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


@pytest.fixture(autouse=True)
def _mock_private_repo_check():
    """POST /config refuses to save unless the repo is confirmed private."""
    with patch(
        "backend.app.services.github_backup.github_backup_service.test_connection",
        new=AsyncMock(
            return_value={
                "success": True,
                "message": "Connection successful",
                "repo_name": "test/repo",
                "permissions": {"push": True},
                "is_private": True,
            }
        ),
    ) as m:
        yield m


async def _create_config(async_client: AsyncClient) -> dict:
    response = await async_client.post(
        "/api/v1/github-backup/config",
        json={
            "repository_url": "https://github.com/test/repo",
            "access_token": "ghp_testtoken123",
            "branch": "main",
            "backup_kprofiles": True,
            "backup_spools": True,
            "backup_archives": True,
            "backup_settings": True,
            "enabled": True,
        },
    )
    assert response.status_code == 200
    return response.json()


class TestCommitsEndpoint:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_404_when_not_configured(self, async_client: AsyncClient):
        response = await async_client.get("/api/v1/github-backup/commits")
        assert response.status_code == 404
        assert "Configure backup first" in response.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_returns_commits_from_the_provider(self, async_client: AsyncClient):
        await _create_config(async_client)
        commits = [
            {"sha": "aaa1111", "message": "Bambuddy backup", "author": "Bambuddy", "date": "2026-07-02T10:00:00Z"}
        ]
        with patch(
            "backend.app.services.git_providers.github.GitHubBackend.list_commits",
            new=AsyncMock(return_value={"success": True, "message": "OK", "commits": commits}),
        ):
            response = await async_client.get("/api/v1/github-backup/commits")

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["branch"] == "main"
        assert body["commits"][0]["sha"] == "aaa1111"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_provider_failure_is_reported_not_raised(self, async_client: AsyncClient):
        await _create_config(async_client)
        with patch(
            "backend.app.services.git_providers.github.GitHubBackend.list_commits",
            new=AsyncMock(return_value={"success": False, "message": "Invalid access token", "commits": []}),
        ):
            response = await async_client.get("/api/v1/github-backup/commits")

        assert response.status_code == 200
        assert response.json()["success"] is False
        assert response.json()["commits"] == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_limit_is_bounded(self, async_client: AsyncClient):
        await _create_config(async_client)
        assert (await async_client.get("/api/v1/github-backup/commits?limit=0")).status_code == 422
        assert (await async_client.get("/api/v1/github-backup/commits?limit=101")).status_code == 422


class TestPreviewEndpoint:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_404_when_not_configured(self, async_client: AsyncClient):
        response = await async_client.get("/api/v1/github-backup/restore/preview")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_reports_available_and_missing_categories(self, async_client: AsyncClient):
        await _create_config(async_client)
        preview = {
            "success": True,
            "message": "OK",
            "ref": "aaa1111",
            "commit": None,
            "metadata_version": "1.0",
            "categories": [
                {"category": "kprofiles", "available": False, "item_count": 0, "detail": "Not present"},
                {"category": "settings", "available": True, "item_count": 12, "detail": None},
                {"category": "spools", "available": True, "item_count": 4, "detail": "plus 9 usage records"},
                {"category": "archives", "available": True, "item_count": 30, "detail": "Metadata only"},
            ],
        }
        with patch(
            "backend.app.services.github_restore.github_restore_service.preview",
            new=AsyncMock(return_value=preview),
        ):
            response = await async_client.get("/api/v1/github-backup/restore/preview?ref=aaa1111")

        assert response.status_code == 200
        body = response.json()
        assert body["metadata_version"] == "1.0"
        by_name = {c["category"]: c for c in body["categories"]}
        assert by_name["kprofiles"]["available"] is False
        assert by_name["spools"]["item_count"] == 4

    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.parametrize("ref", ["main", "abc", "../../etc/passwd", "zzzzzzz"])
    async def test_rejects_refs_that_are_not_object_names(self, async_client: AsyncClient, ref):
        await _create_config(async_client)
        response = await async_client.get(f"/api/v1/github-backup/restore/preview?ref={ref}")
        assert response.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_defaults_to_head(self, async_client: AsyncClient):
        await _create_config(async_client)
        mock = AsyncMock(return_value={"success": True, "message": "OK", "ref": "aaa1111", "categories": []})
        with patch("backend.app.services.github_restore.github_restore_service.preview", new=mock):
            response = await async_client.get("/api/v1/github-backup/restore/preview")

        assert response.status_code == 200
        assert mock.await_args.kwargs["ref"] == "HEAD"


class TestRestoreEndpoint:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_404_when_not_configured(self, async_client: AsyncClient):
        response = await async_client.post("/api/v1/github-backup/restore", json={"categories": ["spools"]})
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_applies_selected_categories(self, async_client: AsyncClient):
        await _create_config(async_client)
        outcome = {
            "success": True,
            "message": "Restored 5 item(s) from aaa1111",
            "log_id": 3,
            "ref": "aaa1111",
            "results": {
                "spools": {"restored": 4, "skipped": 1, "failed": 0, "notes": []},
                "settings": {"restored": 1, "skipped": 2, "failed": 0, "notes": ["1 credential-like key(s) skipped"]},
            },
        }
        with patch(
            "backend.app.services.github_restore.github_restore_service.run_restore",
            new=AsyncMock(return_value=outcome),
        ) as mock:
            response = await async_client.post(
                "/api/v1/github-backup/restore",
                json={"ref": "aaa1111", "categories": ["spools", "settings"], "overwrite_existing": True},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["results"]["spools"]["restored"] == 4
        assert body["results"]["settings"]["notes"] == ["1 credential-like key(s) skipped"]
        assert mock.await_args.kwargs["overwrite_existing"] is True
        assert mock.await_args.kwargs["ref"] == "aaa1111"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rejects_empty_category_list(self, async_client: AsyncClient):
        await _create_config(async_client)
        response = await async_client.post("/api/v1/github-backup/restore", json={"categories": []})
        assert response.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rejects_unknown_category(self, async_client: AsyncClient):
        await _create_config(async_client)
        response = await async_client.post("/api/v1/github-backup/restore", json={"categories": ["cloud_profiles"]})
        assert response.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rejects_malformed_ref(self, async_client: AsyncClient):
        await _create_config(async_client)
        response = await async_client.post(
            "/api/v1/github-backup/restore", json={"ref": "main", "categories": ["spools"]}
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_defaults_overwrite_to_false(self, async_client: AsyncClient):
        """The safe default: a restore only inserts what's missing."""
        await _create_config(async_client)
        mock = AsyncMock(return_value={"success": True, "message": "ok", "results": {}})
        with patch("backend.app.services.github_restore.github_restore_service.run_restore", new=mock):
            response = await async_client.post("/api/v1/github-backup/restore", json={"categories": ["spools"]})

        assert response.status_code == 200
        assert mock.await_args.kwargs["overwrite_existing"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_service_failure_is_reported_in_body(self, async_client: AsyncClient):
        await _create_config(async_client)
        with patch(
            "backend.app.services.github_restore.github_restore_service.run_restore",
            new=AsyncMock(
                return_value={
                    "success": False,
                    "message": "A backup is currently running. Wait for it to finish before restoring.",
                    "results": {},
                }
            ),
        ):
            response = await async_client.post("/api/v1/github-backup/restore", json={"categories": ["spools"]})

        assert response.status_code == 200
        assert response.json()["success"] is False
        assert "backup is currently running" in response.json()["message"]


class TestStatusExposesRestoreState:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_restore_running_is_false_when_idle(self, async_client: AsyncClient):
        await _create_config(async_client)
        response = await async_client.get("/api/v1/github-backup/status")
        assert response.status_code == 200
        assert response.json()["restore_running"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_restore_running_is_reported(self, async_client: AsyncClient):
        """The UI disables both action buttons off this flag."""
        await _create_config(async_client)
        from backend.app.services.github_restore import github_restore_service

        github_restore_service._running_restore = True
        github_restore_service._progress = "Restoring spool inventory..."
        try:
            response = await async_client.get("/api/v1/github-backup/status")
        finally:
            github_restore_service._running_restore = False
            github_restore_service._progress = None

        assert response.json()["restore_running"] is True
        assert response.json()["progress"] == "Restoring spool inventory..."

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unconfigured_status_still_has_the_field(self, async_client: AsyncClient):
        response = await async_client.get("/api/v1/github-backup/status")
        assert response.status_code == 200
        assert response.json()["restore_running"] is False
