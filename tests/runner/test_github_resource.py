"""Tests for :mod:`omnigent.runner.github_resource`.

:func:`github_file_diff` (the on-demand expand-context reader) runs ``git show``
against a real temp repo. The PR-backed :func:`github_changed_files` /
:func:`github_pr_diff` shell out to ``gh``, stubbed here via :func:`_stub_gh`.
:func:`github_info`'s availability fallbacks, its account/remote enumeration, and
its check-summary reducer need neither ``gh`` nor the network.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnigent.runner import github_resource
from omnigent.runner.github_resource import (
    _summarize_checks,
    github_changed_files,
    github_file_diff,
    github_info,
    github_pr_diff,
)


def _stub_gh(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[tuple[str, ...], tuple[int, str, str]],
) -> None:
    """Stub ``github_resource._gh`` to answer by the argv's leading tokens.

    :param responses: Maps a leading-argv prefix (e.g. ``("pr", "view")``) to
        the ``(returncode, stdout, stderr)`` it should return.
    """

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        for prefix, value in responses.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return value
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)


def _git_env() -> dict[str, str]:
    """Env with a dummy git identity so commits don't need a configured user."""
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }


def _run(argv: list[str], cwd: Path) -> None:
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True, env=_git_env())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with a ``main`` base and a ``feature`` branch that adds/edits/deletes.

    ``main``: fileA="A base", fileB="B base", fileC="C base".
    ``feature``: fileA→"A changed", fileB deleted, newfile added, fileC untouched.
    """
    _run(["git", "init"], tmp_path)
    (tmp_path / "fileA.py").write_text("A base")
    (tmp_path / "fileB.py").write_text("B base")
    (tmp_path / "fileC.py").write_text("C base")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "base"], tmp_path)
    _run(["git", "branch", "-M", "main"], tmp_path)

    _run(["git", "checkout", "-b", "feature"], tmp_path)
    (tmp_path / "fileA.py").write_text("A changed")
    (tmp_path / "newfile.py").write_text("new content")
    _run(["git", "rm", "fileB.py"], tmp_path)
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "feature"], tmp_path)
    return tmp_path


def test_github_info_gh_not_installed(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``gh`` there's no PR knowable, so base/pr/repo are null.

    ``available`` still reflects "is a git repo" and reports the branch; the tab
    is a pure PR view, so ``base_ref`` is null until a PR resolves it.
    """
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: None)
    info = github_info(str(repo))
    assert info["available"] is True
    assert info["gh_available"] is False
    assert info["authenticated"] is False
    assert info["branch"] == "feature"
    assert info["base_ref"] is None
    assert info["pr"] is None
    assert info["repo"] is None


def test_github_info_not_a_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-git workspace reports ``not_a_git_repo`` regardless of ``gh``."""
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")
    info = github_info(str(tmp_path))
    assert info["available"] is False
    assert info["reason"] == "not_a_git_repo"


def test_github_info_pr_via_pr_view(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The PR resolves in one bare ``gh pr view --json`` — fork heads included.

    With the base repo and account pinned, ``gh`` resolves the current branch's
    PR itself (fork / triangular ``alice/feature`` head and all), so there's no
    head-ref heuristic and never a ``gh pr list`` call.
    """
    pr = {
        "number": 42,
        "title": "Add thing",
        "state": "OPEN",
        "url": "https://github.com/acme/repo/pull/42",
        "isDraft": True,
        "author": {"login": "alice"},
        "baseRefName": "main",
        "headRefName": "alice/feature",
        "statusCheckRollup": [],
    }
    calls: list[tuple[str, ...]] = []

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        calls.append(tuple(argv))
        head = tuple(argv[:2])
        if head == ("auth", "status"):
            return (0, "", "")
        if head == ("repo", "view"):
            return (0, json.dumps({"nameWithOwner": "acme/repo"}), "")
        if head == ("pr", "view"):
            return (0, json.dumps(pr), "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")

    info = github_info(str(repo))
    assert info["pr"]["number"] == 42
    assert info["pr"]["head_ref"] == "alice/feature"
    assert info["pr"]["is_draft"] is True
    assert info["base_ref"] == "main"
    assert any(c[:2] == ("pr", "view") for c in calls)
    assert not any(c[:2] == ("pr", "list") for c in calls)


def test_github_info_no_pr(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A branch with no PR yields ``pr``/``base_ref`` null (bare ``gh pr view`` empty)."""

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        head = tuple(argv[:2])
        if head == ("auth", "status"):
            return (0, "", "")
        if head == ("repo", "view"):
            return (0, json.dumps({"nameWithOwner": "o/r"}), "")
        if head == ("pr", "view"):
            return (1, "", "no pull requests found for branch")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")

    info = github_info(str(repo))
    assert info["pr"] is None
    assert info["base_ref"] is None


def test_github_info_enumerates_accounts_and_remotes(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """github_info lists gh accounts and git remotes for the panel's selectors."""
    _run(["git", "remote", "add", "origin", "https://github.com/acme/repo.git"], repo)
    _run(["git", "remote", "add", "fork", "git@github.com:alice/repo.git"], repo)
    hosts = {
        "hosts": {
            "github.com": [
                {"login": "alice", "active": True, "state": "success"},
                {"login": "bob", "active": False, "state": "success"},
            ]
        }
    }

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        if list(argv[:4]) == ["auth", "status", "--json", "hosts"]:
            return (0, json.dumps(hosts), "")
        if tuple(argv[:2]) == ("repo", "view"):
            return (0, json.dumps({"nameWithOwner": "acme/repo"}), "")
        if tuple(argv[:2]) == ("pr", "view"):
            return (1, "", "no pr")
        return (1, "", "no stub")  # set-default --view: no default set

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")

    info = github_info(str(repo))
    assert info["authenticated"] is True
    assert {a["login"] for a in info["accounts"]} == {"alice", "bob"}
    # No stored preference and no resolved base → the active account is selected.
    assert info["selected_account"] == "alice"
    remotes = {r["name"]: r["owner_repo"] for r in info["remotes"]}
    assert remotes == {"origin": "acme/repo", "fork": "alice/repo"}


def test_github_info_runs_gh_as_preferred_account(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored per-repo account is applied as GH_TOKEN on the API-touching calls."""
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    monkeypatch.setattr(github_resource, "_resolved_base_nwo", lambda _root: "acme/repo")
    monkeypatch.setattr(
        github_resource._config,
        "github_account_preference",
        lambda nwo: "bob" if nwo == "acme/repo" else None,
    )
    monkeypatch.setattr(github_resource, "_gh_auth_token", lambda _root, login: f"tok-{login}")
    seen: dict[str, str | None] = {}

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        if list(argv[:4]) == ["auth", "status", "--json", "hosts"]:
            bob = {"login": "bob", "active": False, "state": "success"}
            return (0, json.dumps({"hosts": {"github.com": [bob]}}), "")
        if tuple(argv[:2]) == ("repo", "view"):
            seen["repo_view"] = token
            return (0, json.dumps({"nameWithOwner": "acme/repo"}), "")
        if tuple(argv[:2]) == ("pr", "view"):
            seen["pr_view"] = token
            return (1, "", "no pr")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")

    info = github_info(str(repo))
    assert info["selected_account"] == "bob"
    assert seen["repo_view"] == "tok-bob"
    assert seen["pr_view"] == "tok-bob"


def test_github_changed_files_via_pr_view(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The changed-files list resolves the PR number via bare ``gh pr view``, then fetches."""
    files = [{"filename": "a.py", "status": "added", "additions": 1, "deletions": 0}]

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        if tuple(argv[:2]) == ("pr", "view"):
            return (0, json.dumps({"number": 9}), "")
        if argv and argv[0] == "api":
            return (0, json.dumps(files), "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    result = github_changed_files(str(repo))
    assert [entry["path"] for entry in result["data"]] == ["a.py"]


def test_github_changed_files_maps_pr_file_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The list comes from ``gh api pulls/<n>/files``, mapping GitHub statuses."""
    files = [
        {"filename": "newfile.py", "status": "added", "additions": 1, "deletions": 0},
        {"filename": "src/fileA.py", "status": "modified", "additions": 2, "deletions": 1},
        {"filename": "fileB.py", "status": "removed", "additions": 0, "deletions": 3},
        {
            "filename": "new/name.py",
            "status": "renamed",
            "additions": 0,
            "deletions": 0,
            "previous_filename": "old/name.py",
        },
    ]
    _stub_gh(
        monkeypatch,
        {
            ("pr", "view"): (0, json.dumps({"number": 7}), ""),
            ("api",): (0, json.dumps(files), ""),
        },
    )
    result = github_changed_files("/root")
    by_path = {entry["path"]: entry for entry in result["data"]}
    assert by_path["newfile.py"]["status"] == "created"
    assert by_path["src/fileA.py"]["status"] == "modified"
    assert by_path["fileB.py"]["status"] == "deleted"
    assert by_path["new/name.py"]["status"] == "renamed"
    # Line counts and the display name come straight from the PR file entry.
    assert by_path["newfile.py"]["lines_added"] == 1
    assert by_path["src/fileA.py"]["name"] == "fileA.py"


def test_github_file_diff_added(repo: Path) -> None:
    """An added file has no base content but the new HEAD content."""
    diff = github_file_diff(str(repo), "main", "newfile.py")
    assert diff["before"] is None
    assert diff["after"] == "new content"


def test_github_file_diff_modified(repo: Path) -> None:
    """A modified file shows base content as before and HEAD content as after."""
    diff = github_file_diff(str(repo), "main", "fileA.py")
    assert diff["before"] == "A base"
    assert diff["after"] == "A changed"


def test_github_file_diff_deleted(repo: Path) -> None:
    """A deleted file shows base content as before and None as after."""
    diff = github_file_diff(str(repo), "main", "fileB.py")
    assert diff["before"] == "B base"
    assert diff["after"] is None


def test_github_changed_files_no_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no PR for the branch, the list is empty (no local git fallback)."""
    _stub_gh(monkeypatch, {("pr", "view"): (1, "", "no pull requests found")})
    assert github_changed_files("/root") == {"object": "list", "data": [], "has_more": False}


def test_github_pr_diff_returns_gh_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole-PR patch is ``gh pr diff <number>`` verbatim (GitHub-computed)."""
    patch = "diff --git a/fileA.py b/fileA.py\n@@ -1 +1 @@\n-A base\n+A changed\n"
    _stub_gh(
        monkeypatch,
        {
            ("pr", "view"): (0, json.dumps({"number": 7}), ""),
            ("pr", "diff"): (0, patch, ""),
        },
    )
    assert github_pr_diff("/root") == {"object": "session.github.pr_diff", "patch": patch}


def test_github_pr_diff_no_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no PR for the branch, the patch is empty rather than an error."""
    _stub_gh(monkeypatch, {("pr", "view"): (1, "", "no pull requests found")})
    assert github_pr_diff("/root") == {"object": "session.github.pr_diff", "patch": ""}


def test_github_pr_diff_resolves_number_then_diffs(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole-PR diff resolves the number via ``gh pr view``, then ``gh pr diff <n>``."""
    patch = "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    calls: list[tuple[str, ...]] = []

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        calls.append(tuple(argv))
        head = tuple(argv[:2])
        if head == ("pr", "view"):
            return (0, json.dumps({"number": 9}), "")
        if head == ("pr", "diff"):
            return (0, patch, "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    result = github_pr_diff(str(repo))
    assert result == {"object": "session.github.pr_diff", "patch": patch}
    # The diff was fetched by the resolved number, never a bare 'gh pr diff'.
    assert [c for c in calls if c[:2] == ("pr", "diff")] == [("pr", "diff", "9")]


# ── Account + remote selection ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/repo.git", "acme/repo"),
        ("https://github.com/acme/repo", "acme/repo"),
        ("https://user@github.com/acme/repo.git", "acme/repo"),
        ("git@github.com:alice/repo.git", "alice/repo"),
        ("ssh://git@github.com/acme/repo.git", "acme/repo"),
        ("not a url", None),
        ("", None),
        (None, None),
    ],
)
def test_owner_repo_from_url(url: str | None, expected: str | None) -> None:
    """Owner/repo parsing handles HTTPS / SSH / scp forms and rejects junk."""
    assert github_resource._owner_repo_from_url(url) == expected


def test_list_remotes_parses_fetch_push(repo: Path) -> None:
    """`git remote -v` is grouped by name into {name, owner_repo}."""
    _run(["git", "remote", "add", "origin", "https://github.com/acme/repo.git"], repo)
    _run(["git", "remote", "add", "fork", "git@github.com:alice/repo.git"], repo)
    by_name = {r["name"]: r["owner_repo"] for r in github_resource._list_remotes(str(repo))}
    assert by_name == {"origin": "acme/repo", "fork": "alice/repo"}


def test_list_accounts_parses_hosts_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gh auth status --json hosts` yields the accounts and the authed boolean."""
    hosts = {
        "hosts": {
            "github.com": [
                {"login": "alice", "active": True, "state": "success"},
                {"login": "bob", "active": False, "state": "success"},
            ]
        }
    }

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        if list(argv[:4]) == ["auth", "status", "--json", "hosts"]:
            return (0, json.dumps(hosts), "")
        return (1, "", "")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    authed, accounts = github_resource._list_accounts("/root")
    assert authed is True
    assert [a["login"] for a in accounts] == ["alice", "bob"]
    assert accounts[0]["active"] is True


def test_list_accounts_falls_back_on_old_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """An old gh without ``--json hosts`` → plain ``gh auth status`` decides the bool."""

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        if "--json" in argv:
            return (1, "", "unknown flag: --json")
        if tuple(argv[:2]) == ("auth", "status"):
            return (0, "", "")
        return (1, "", "")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    authed, accounts = github_resource._list_accounts("/root")
    assert authed is True
    assert accounts == []


def test_account_token_for_none_in_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sandbox keeps its single broker identity — never a per-repo token override."""
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setattr(github_resource, "_resolved_base_nwo", lambda _root: "o/r")
    monkeypatch.setattr(github_resource._config, "github_account_preference", lambda _nwo: "bob")
    assert github_resource._account_token_for("/root") is None


def test_account_token_for_none_without_preference(monkeypatch: pytest.MonkeyPatch) -> None:
    """No stored preference → no override (gh's active account is used)."""
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    monkeypatch.setattr(github_resource, "_resolved_base_nwo", lambda _root: "o/r")
    monkeypatch.setattr(github_resource._config, "github_account_preference", lambda _nwo: None)
    assert github_resource._account_token_for("/root") is None


def test_set_github_preference_sets_default_and_account(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selection runs ``gh repo set-default`` (remote) and stores the account pref."""
    calls: list[tuple[str, ...]] = []
    saved: dict[str, str | None] = {}
    monkeypatch.setattr(github_resource, "_resolved_base_nwo", lambda _root: "acme/repo")
    monkeypatch.setattr(
        github_resource._config,
        "set_github_account_preference",
        lambda nwo, login, *a, **k: saved.update({"nwo": nwo, "login": login}),
    )
    monkeypatch.setattr(github_resource, "github_info", lambda _root: {"stub": True})

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        calls.append(tuple(argv))
        return (0, "", "")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    out = github_resource.set_github_preference(str(repo), account="bob", remote="fork")
    assert ("repo", "set-default", "fork") in calls
    assert saved == {"nwo": "acme/repo", "login": "bob"}
    assert out == {"stub": True}


# ── _gh environment handling ─────────────────────────────────────────────────


def test_summarize_checks_mixed() -> None:
    """The reducer classifies CheckRun (status/conclusion) and StatusContext (state)."""
    rollup = [
        {"name": "unit", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": "u"},
        {"name": "e2e", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"workflowName": "bench", "status": "IN_PROGRESS", "conclusion": None},
        {"context": "legacy-ok", "state": "SUCCESS", "targetUrl": "t"},
        {"context": "legacy-wait", "state": "PENDING"},
        {"context": "legacy-err", "state": "ERROR"},
    ]
    result = _summarize_checks(rollup)
    assert result["passing"] == 2
    assert result["failing"] == 2
    assert result["pending"] == 2
    assert result["total"] == 6
    # Per-check details carry the job name, bucket, and link (name falls back to
    # context / workflowName; url falls back to targetUrl).
    assert {"name": "unit", "bucket": "passing", "url": "u"} in result["runs"]
    assert {"name": "e2e", "bucket": "failing", "url": None} in result["runs"]
    assert {"name": "bench", "bucket": "pending", "url": None} in result["runs"]
    assert {"name": "legacy-ok", "bucket": "passing", "url": "t"} in result["runs"]


def test_summarize_checks_empty() -> None:
    """A missing/empty rollup summarizes to all zeros with no runs."""
    assert _summarize_checks(None) == {
        "passing": 0,
        "failing": 0,
        "pending": 0,
        "total": 0,
        "runs": [],
    }


def test_gh_scrubs_env_tokens_in_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    # In a sandbox the panel's gh must authenticate as the connected owner via
    # hosts.yml, never an ambient GH_TOKEN/GITHUB_TOKEN (gh ranks those above
    # hosts.yml) — so they're scrubbed from gh's env, restoring the fail-closed
    # property and preventing a stray token from making the panel a shared identity.
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv("GH_TOKEN", "shared-tok")
    monkeypatch.setenv("GITHUB_TOKEN", "shared-tok")
    captured: dict[str, object] = {}

    def fake_run(argv: Sequence[str], *, cwd: str, timeout: float, env=None):
        captured["argv"] = list(argv)
        captured["env"] = env
        return 0, "", ""

    monkeypatch.setattr(github_resource, "_run", fake_run)
    github_resource._gh(["api", "user"], cwd="/tmp")
    assert captured["argv"] == ["gh", "api", "user"]
    env = captured["env"]
    assert isinstance(env, dict)
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env


def test_gh_inherits_env_outside_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    # Local dev (not a sandbox): env is inherited untouched (env=None), so the
    # developer's own gh auth / GH_TOKEN keeps working — no regression.
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    captured: dict[str, object] = {}

    def fake_run(argv: Sequence[str], *, cwd: str, timeout: float, env=None):
        captured["env"] = env
        return 0, "", ""

    monkeypatch.setattr(github_resource, "_run", fake_run)
    github_resource._gh(["pr", "diff"], cwd="/tmp")
    assert captured["env"] is None


def test_gh_applies_account_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # An explicit token runs this one call as the selected account: GH_TOKEN is
    # set to it and any stray GITHUB_TOKEN is dropped so it can't win.
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "stray")
    captured: dict[str, object] = {}

    def fake_run(argv: Sequence[str], *, cwd: str, timeout: float, env=None):
        captured["env"] = env
        return 0, "", ""

    monkeypatch.setattr(github_resource, "_run", fake_run)
    github_resource._gh(["pr", "view"], cwd="/tmp", token="chosen-tok")
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["GH_TOKEN"] == "chosen-tok"
    assert "GITHUB_TOKEN" not in env
