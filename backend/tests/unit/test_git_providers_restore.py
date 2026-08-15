"""Unit tests for the git_providers read side used by restore (#2656).

Covers list_commits / list_tree / fetch_files across all four providers,
including that Gitea and Forgejo inherit GitHub's Git Data API implementation
rather than needing their own.
"""

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.services.git_providers.forgejo import ForgejoBackend
from backend.app.services.git_providers.gitea import GiteaBackend
from backend.app.services.git_providers.github import GitHubBackend
from backend.app.services.git_providers.gitlab import GitLabBackend


def _make_mock_response(status_code: int, body=None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json = MagicMock(return_value=body if body is not None else {})
    return resp


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode()


def _github_commit(sha: str, message: str = "Bambuddy backup", date: str = "2026-07-01T10:00:00Z"):
    return {"sha": sha, "commit": {"message": message, "author": {"name": "Bambuddy", "date": date}}}


class TestGitHubListCommits:
    def setup_method(self):
        self.backend = GitHubBackend()
        self.repo_url = "https://github.com/owner/repo"
        self.token = "ghp_token"

    @pytest.mark.asyncio
    async def test_returns_normalised_commits_newest_first(self):
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_make_mock_response(
                200,
                [
                    _github_commit("aaa111", "Bambuddy backup - newest", "2026-07-02T10:00:00Z"),
                    _github_commit("bbb222", "Bambuddy backup - older", "2026-07-01T10:00:00Z"),
                ],
            )
        )

        result = await self.backend.list_commits(self.repo_url, self.token, "main", client)

        assert result["success"] is True
        assert [c["sha"] for c in result["commits"]] == ["aaa111", "bbb222"]
        assert result["commits"][0]["message"] == "Bambuddy backup - newest"
        assert result["commits"][0]["author"] == "Bambuddy"
        assert result["commits"][0]["date"] == "2026-07-02T10:00:00Z"

    @pytest.mark.asyncio
    async def test_sends_both_per_page_and_limit(self):
        """GitHub honours per_page, Gitea honours limit — one call must carry both
        so GiteaBackend can inherit this method unchanged."""
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_mock_response(200, []))

        await self.backend.list_commits(self.repo_url, self.token, "main", client, limit=7)

        params = client.get.await_args.kwargs["params"]
        assert params["per_page"] == 7
        assert params["limit"] == 7
        assert params["sha"] == "main"

    @pytest.mark.asyncio
    async def test_respects_limit_even_if_provider_overshoots(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_mock_response(200, [_github_commit(f"sha{i}") for i in range(10)]))

        result = await self.backend.list_commits(self.repo_url, self.token, "main", client, limit=3)

        assert len(result["commits"]) == 3

    @pytest.mark.asyncio
    async def test_404_explains_empty_repository(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_mock_response(404, {}))

        result = await self.backend.list_commits(self.repo_url, self.token, "nope", client)

        assert result["success"] is False
        assert "no commits yet" in result["message"]
        assert result["commits"] == []

    @pytest.mark.asyncio
    async def test_skips_entries_without_a_sha(self):
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_make_mock_response(200, [{"commit": {"message": "no sha"}}, _github_commit("good")])
        )

        result = await self.backend.list_commits(self.repo_url, self.token, "main", client)

        assert [c["sha"] for c in result["commits"]] == ["good"]

    @pytest.mark.asyncio
    async def test_non_list_body_is_an_error_not_a_crash(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_mock_response(200, {"unexpected": "shape"}))

        result = await self.backend.list_commits(self.repo_url, self.token, "main", client)

        assert result["success"] is False
        assert "Unexpected shape" in result["message"]


class TestGitHubListTree:
    def setup_method(self):
        self.backend = GitHubBackend()
        self.repo_url = "https://github.com/owner/repo"
        self.token = "ghp_token"

    @pytest.mark.asyncio
    async def test_returns_sorted_blob_paths_only(self):
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_make_mock_response(
                200,
                {
                    "tree": [
                        {"type": "blob", "path": "spools/inventory.json", "sha": "s1"},
                        {"type": "tree", "path": "spools", "sha": "d1"},
                        {"type": "blob", "path": "backup_metadata.json", "sha": "m1"},
                    ]
                },
            )
        )

        result = await self.backend.list_tree(self.repo_url, self.token, "abc1234", client)

        assert result["success"] is True
        assert result["paths"] == ["backup_metadata.json", "spools/inventory.json"]

    @pytest.mark.asyncio
    async def test_truncated_tree_fails_loudly(self):
        """A truncated listing would make restore silently miss categories."""
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_mock_response(200, {"tree": [], "truncated": True}))

        result = await self.backend.list_tree(self.repo_url, self.token, "abc1234", client)

        assert result["success"] is False
        assert "truncated" in result["message"]

    @pytest.mark.asyncio
    async def test_404_names_the_missing_ref(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_mock_response(404, {}))

        result = await self.backend.list_tree(self.repo_url, self.token, "deadbee", client)

        assert result["success"] is False
        assert "deadbee" in result["message"]


class TestGitHubFetchFiles:
    def setup_method(self):
        self.backend = GitHubBackend()
        self.repo_url = "https://github.com/owner/repo"
        self.token = "ghp_token"

    @pytest.mark.asyncio
    async def test_reads_requested_paths_via_blob_api(self):
        tree = _make_mock_response(
            200,
            {
                "tree": [
                    {"type": "blob", "path": "a.json", "sha": "sha-a"},
                    {"type": "blob", "path": "b.json", "sha": "sha-b"},
                ]
            },
        )
        client = AsyncMock()
        client.get = AsyncMock(
            side_effect=[
                tree,
                _make_mock_response(200, {"content": _b64('{"a": 1}'), "encoding": "base64"}),
            ]
        )

        result = await self.backend.fetch_files(self.repo_url, self.token, "abc1234", ["a.json"], client)

        assert result["success"] is True
        assert result["files"] == {"a.json": '{"a": 1}'}
        # One tree listing regardless of how many files are read.
        assert client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_lists_the_tree_once_for_many_files(self):
        tree = _make_mock_response(
            200,
            {
                "tree": [
                    {"type": "blob", "path": "a.json", "sha": "sha-a"},
                    {"type": "blob", "path": "b.json", "sha": "sha-b"},
                ]
            },
        )
        client = AsyncMock()
        client.get = AsyncMock(
            side_effect=[
                tree,
                _make_mock_response(200, {"content": _b64("1"), "encoding": "base64"}),
                _make_mock_response(200, {"content": _b64("2"), "encoding": "base64"}),
            ]
        )

        result = await self.backend.fetch_files(self.repo_url, self.token, "abc1234", ["a.json", "b.json"], client)

        assert result["files"] == {"a.json": "1", "b.json": "2"}
        assert client.get.await_count == 3

    @pytest.mark.asyncio
    async def test_missing_path_is_skipped_not_an_error(self):
        """Which categories a backup contains varies by config, so an absent
        path is expected rather than a failure."""
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_mock_response(200, {"tree": []}))

        result = await self.backend.fetch_files(self.repo_url, self.token, "abc1234", ["gone.json"], client)

        assert result["success"] is True
        assert result["files"] == {}

    @pytest.mark.asyncio
    async def test_blob_error_fails_the_whole_read(self):
        tree = _make_mock_response(200, {"tree": [{"type": "blob", "path": "a.json", "sha": "sha-a"}]})
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[tree, _make_mock_response(500, {}, text="boom")])

        result = await self.backend.fetch_files(self.repo_url, self.token, "abc1234", ["a.json"], client)

        assert result["success"] is False
        assert "a.json" in result["message"]
        assert result["files"] == {}

    @pytest.mark.asyncio
    async def test_utf8_content_survives_round_trip(self):
        payload = '{"color_name": "Jadeweiß", "note": "日本語"}'
        tree = _make_mock_response(200, {"tree": [{"type": "blob", "path": "a.json", "sha": "sha-a"}]})
        client = AsyncMock()
        client.get = AsyncMock(
            side_effect=[tree, _make_mock_response(200, {"content": _b64(payload), "encoding": "base64"})]
        )

        result = await self.backend.fetch_files(self.repo_url, self.token, "abc1234", ["a.json"], client)

        assert result["files"]["a.json"] == payload

    @pytest.mark.asyncio
    async def test_unsupported_encoding_is_reported(self):
        tree = _make_mock_response(200, {"tree": [{"type": "blob", "path": "a.json", "sha": "sha-a"}]})
        client = AsyncMock()
        client.get = AsyncMock(
            side_effect=[tree, _make_mock_response(200, {"content": "xx", "encoding": "quoted-printable"})]
        )

        result = await self.backend.fetch_files(self.repo_url, self.token, "abc1234", ["a.json"], client)

        assert result["success"] is False
        assert "Unsupported blob encoding" in result["message"]


class TestGiteaAndForgejoInheritReads:
    """Gitea overrides the *write* path only; reads come from GitHubBackend."""

    @pytest.mark.parametrize("backend_cls", [GiteaBackend, ForgejoBackend])
    def test_read_methods_are_not_overridden(self, backend_cls):
        for method in ("list_commits", "list_tree", "fetch_files"):
            assert getattr(backend_cls, method) is getattr(GitHubBackend, method)

    @pytest.mark.asyncio
    async def test_gitea_list_commits_uses_its_own_api_base(self):
        backend = GiteaBackend()
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_mock_response(200, [_github_commit("abc")]))

        result = await backend.list_commits("https://git.example.com/owner/repo", "tok", "main", client)

        assert result["success"] is True
        url = client.get.await_args.args[0]
        assert url.startswith("https://git.example.com/api/v1/repos/owner/repo/commits")

    @pytest.mark.asyncio
    async def test_gitea_subpath_install_is_respected(self):
        """Gitea/Forgejo behind a ROOT_URL sub-path (#2642)."""
        backend = GiteaBackend()
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_mock_response(200, {"tree": []}))

        await backend.list_tree("https://example.com/git/owner/repo", "tok", "abc1234", client)

        url = client.get.await_args.args[0]
        assert "/git/api/v1/repos/owner/repo/git/trees/abc1234" in url


class TestGitLabReads:
    def setup_method(self):
        self.backend = GitLabBackend()
        self.repo_url = "https://gitlab.com/owner/repo"
        self.token = "glpat-test"

    @pytest.mark.asyncio
    async def test_list_commits_reads_flattened_author_fields(self):
        """GitLab puts message/author/date on the entry, not under 'commit'."""
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_make_mock_response(
                200,
                [
                    {
                        "id": "abc123",
                        "message": "Bambuddy backup",
                        "author_name": "Bambuddy",
                        "committed_date": "2026-07-02T10:00:00Z",
                    }
                ],
            )
        )

        result = await self.backend.list_commits(self.repo_url, self.token, "main", client)

        assert result["success"] is True
        assert result["commits"] == [
            {
                "sha": "abc123",
                "message": "Bambuddy backup",
                "author": "Bambuddy",
                "date": "2026-07-02T10:00:00Z",
            }
        ]

    @pytest.mark.asyncio
    async def test_list_commits_uses_ref_name(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_mock_response(200, []))

        await self.backend.list_commits(self.repo_url, self.token, "bambuddy-backup", client, limit=5)

        params = client.get.await_args.kwargs["params"]
        assert params["ref_name"] == "bambuddy-backup"
        assert params["per_page"] == 5

    @pytest.mark.asyncio
    async def test_subgroup_path_is_url_encoded(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_mock_response(200, []))

        await self.backend.list_commits("https://gitlab.com/group/subgroup/proj", self.token, "main", client)

        url = client.get.await_args.args[0]
        assert "projects/group%2Fsubgroup%2Fproj/repository/commits" in url

    @pytest.mark.asyncio
    async def test_list_tree_returns_blob_paths(self):
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_make_mock_response(
                200,
                [
                    {"type": "blob", "path": "spools/inventory.json"},
                    {"type": "tree", "path": "spools"},
                ],
            )
        )

        result = await self.backend.list_tree(self.repo_url, self.token, "abc1234", client)

        assert result["success"] is True
        assert result["paths"] == ["spools/inventory.json"]

    @pytest.mark.asyncio
    async def test_list_tree_follows_pagination(self):
        """GitLab paginates instead of exposing a truncated flag."""
        full_page = [{"type": "blob", "path": f"f{i}.json"} for i in range(100)]
        client = AsyncMock()
        client.get = AsyncMock(
            side_effect=[
                _make_mock_response(200, full_page),
                _make_mock_response(200, [{"type": "blob", "path": "last.json"}]),
            ]
        )

        result = await self.backend.list_tree(self.repo_url, self.token, "abc1234", client)

        assert client.get.await_count == 2
        assert len(result["paths"]) == 101
        assert "last.json" in result["paths"]

    @pytest.mark.asyncio
    async def test_fetch_files_decodes_base64(self):
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_make_mock_response(200, {"content": _b64('{"k": 1}'), "encoding": "base64"})
        )

        result = await self.backend.fetch_files(self.repo_url, self.token, "abc1234", ["a.json"], client)

        assert result["success"] is True
        assert result["files"] == {"a.json": '{"k": 1}'}

    @pytest.mark.asyncio
    async def test_fetch_files_encodes_nested_path(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_mock_response(200, {"content": _b64("{}"), "encoding": "base64"}))

        await self.backend.fetch_files(self.repo_url, self.token, "abc1234", ["spools/inventory.json"], client)

        url = client.get.await_args.args[0]
        assert "repository/files/spools%2Finventory.json" in url

    @pytest.mark.asyncio
    async def test_fetch_files_skips_404(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_mock_response(404, {}))

        result = await self.backend.fetch_files(self.repo_url, self.token, "abc1234", ["gone.json"], client)

        assert result["success"] is True
        assert result["files"] == {}
