"""GitHub integration for the session workspace, backed by the ``gh`` CLI.

Powers the web UI's read-only "GitHub" rail tab, which is purely a PR view: the
changed-files list and the whole-PR patch come straight from GitHub via ``gh``
(``gh api .../pulls/<n>/files`` and ``gh pr diff``), so they match the PR's
"Files changed" exactly. With no PR for the branch the tab shows its "no PR"
empty state and fetches nothing.

Design notes:

- Commands run via plain :func:`subprocess.run` in the workspace root, NOT the
  sandboxed OS-env shell helper. The helper strips secrets from the environment,
  which would break ``gh`` auth; a plain subprocess inherits the runner process
  environment, so ``gh`` authenticates as it normally does — the developer's
  ``gh`` login in local dev, and in a managed sandbox the per-user ``hosts.yml``
  that :func:`omnigent.git_credential_github.configure_host_gh` writes from the
  credential broker at host startup. In a sandbox we additionally scrub
  ``GH_TOKEN``/``GITHUB_TOKEN`` from ``gh``'s env (gh ranks those *above*
  ``hosts.yml``), so a stray ambient token — e.g. a gh-MCP env passthrough —
  can't silently make the panel act as a shared identity instead of the
  connected owner. Outside a sandbox the env is inherited untouched.
- The list and patch are GitHub-computed, never a local ``git diff``, so a stale
  local ``origin/<base>`` can't inflate them with files outside the PR.
- Only the on-demand per-file expand-context reader (:func:`github_file_diff`)
  still uses ``git show`` for full before/after content — a unified-diff blob
  can't drive the viewer's context expansion.
- The branch→PR lookup is a single ``gh pr view --json`` (``--json`` avoids the
  interactive pager and the Projects-classic mis-parse of a bare view). Fork /
  triangular PRs resolve with no head-ref heuristic once the two coordinates they
  turn on are set explicitly: the base repo (``gh repo set-default``, which ``gh``
  stores in ``.git/config`` as ``remote.<name>.gh-resolved``) and the head owner
  (the authenticated account). The panel surfaces an account + remote selector so
  the user pins both; a per-repo account preference (``~/.omnigent/config.yaml``)
  is applied by running each ``gh`` call as the chosen account —
  ``GH_TOKEN=$(gh auth token --user <login>)`` — outside a sandbox (inside one we
  keep the single broker identity).
- ``available: false`` payloads let the tab render a message ("gh not installed",
  "not a git repo") instead of surfacing an error.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any

from omnigent import config as _config
from omnigent.runtime.filesystem_registry import _git_timeout_seconds

_logger = logging.getLogger(__name__)

# ``gh pr view`` / ``gh repo view`` reach the GitHub API, so they get their own,
# slightly more generous timeout than the local ``git`` reads. Overridable via
# ``OMNIGENT_GH_TIMEOUT_SECONDS`` so operators can tune it without a restart.
_DEFAULT_GH_TIMEOUT_SECONDS = 15.0

# Fields requested from ``gh pr view``. Always pass ``--json`` — bare
# ``gh pr view`` opens an interactive/pager view and misbehaves in a
# non-interactive subprocess.
_PR_VIEW_FIELDS = "number,title,state,url,isDraft,author,baseRefName,headRefName,statusCheckRollup"


def _gh_timeout_seconds() -> float:
    """Return the ``gh``-subprocess timeout, honoring the env override."""
    raw = os.environ.get("OMNIGENT_GH_TIMEOUT_SECONDS")
    if raw is not None:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    return _DEFAULT_GH_TIMEOUT_SECONDS


def _run(
    argv: list[str],
    *,
    cwd: str,
    timeout: float,
    env: dict[str, str] | None = None,
) -> tuple[int | None, str, str]:
    """Run a subprocess and capture its output, never raising.

    :param argv: Command and arguments.
    :param cwd: Working directory to run in.
    :param timeout: Wall-clock cap in seconds.
    :param env: Full child environment, or ``None`` to inherit this process's
        (the default).
    :returns: ``(returncode, stdout, stderr)``. ``returncode`` is ``None`` when
        the command could not run at all (spawn error / timeout), so callers can
        distinguish "ran and failed" from "never ran".
    """
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        _logger.warning(
            "github_resource: %r in %s timed out after %.2fs",
            argv,
            cwd,
            time.monotonic() - started,
        )
        return None, "", "timed out"
    except OSError as exc:
        _logger.warning("github_resource: %r in %s could not run: %s", argv, cwd, exc)
        return None, "", str(exc)
    return (
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


def _git(argv: list[str], *, cwd: str) -> tuple[int | None, str, str]:
    return _run(["git", *argv], cwd=cwd, timeout=_git_timeout_seconds())


def _in_sandbox() -> bool:
    """Whether the panel is running inside a managed sandbox (``IS_SANDBOX=1``)."""
    return (os.environ.get("IS_SANDBOX") or "").strip() == "1"


def _gh(argv: list[str], *, cwd: str, token: str | None = None) -> tuple[int | None, str, str]:
    # In a managed sandbox the panel must authenticate as the connected owner via
    # the per-user hosts.yml that configure_host_gh writes — never an ambient
    # GH_TOKEN/GITHUB_TOKEN, which gh ranks ABOVE hosts.yml. Scrub them so a stray
    # token in the sandbox/runner env (e.g. a gh-MCP passthrough) can't silently
    # make the panel act as a shared identity. Outside a sandbox (local dev) the
    # env is inherited untouched, so the developer's own gh auth still works.
    #
    # ``token`` deliberately re-adds GH_TOKEN to run this one call as a chosen
    # account (the account selector). It's only ever set outside a sandbox — see
    # _account_token_for — so it never overrides the sandbox's broker identity.
    env: dict[str, str] | None = None
    if _in_sandbox():
        env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    if token:
        env = dict(os.environ) if env is None else env
        env["GH_TOKEN"] = token
        env.pop("GITHUB_TOKEN", None)
    return _run(["gh", *argv], cwd=cwd, timeout=_gh_timeout_seconds(), env=env)


# ── Account + remote selection ───────────────────────────────────────────────
# The panel offers two selectors — a remote (the base repo) and an account (the
# head owner) — so fork/triangular PRs resolve without any head-ref heuristic:
# ``gh repo set-default`` pins the base in git config, and a per-repo account
# preference runs every ``gh`` call as the chosen login. Both enumerations are
# local (no network); a sandbox has a single identity so the account arm no-ops.

# gh's own remote line shape (see cli/cli git/client.go): ``name url (fetch|push)``.
_REMOTE_LINE_RE = re.compile(r"^(\S+)\s+(\S+)\s+\((fetch|push)\)$")


def _owner_repo_from_url(url: str | None) -> str | None:
    """Derive ``owner/repo`` from a git remote URL, or ``None``.

    Handles HTTPS/SSH/scp-style GitHub URLs, stripping any ``.git`` suffix.
    """
    if not url:
        return None
    candidate = url.strip()
    scp = re.match(r"^[\w.\-]+@[\w.\-]+:(?P<path>.+)$", candidate)
    if scp:
        path = scp.group("path")
    else:
        scheme = re.match(r"^\w+://(?:[^@/]+@)?[\w.\-]+/(?P<path>.+)$", candidate)
        if not scheme:
            return None
        path = scheme.group("path")
    parts = path.removesuffix(".git").strip("/").split("/")
    if len(parts) < 2 or not parts[-1] or not parts[-2]:
        return None
    return f"{parts[-2]}/{parts[-1]}"


def _list_remotes(root: str) -> list[dict[str, Any]]:
    """List the workspace's git remotes as ``{name, owner_repo}`` (empty on error).

    Parses ``git remote -v`` the way ``gh`` does, grouping the fetch/push lines by
    name and deriving ``owner/repo`` (fetch URL preferred) for the base selector.
    """
    rc, out, _ = _git(["remote", "-v"], cwd=root)
    if rc != 0:
        return []
    grouped: dict[str, dict[str, str | None]] = {}
    for line in out.splitlines():
        match = _REMOTE_LINE_RE.match(line.strip())
        if not match:
            continue
        name, remote_url, kind = match.group(1), match.group(2), match.group(3)
        entry = grouped.setdefault(name, {"fetch": None, "push": None})
        entry[kind] = remote_url
    remotes: list[dict[str, Any]] = []
    for name, urls in grouped.items():
        remotes.append(
            {"name": name, "owner_repo": _owner_repo_from_url(urls["fetch"] or urls["push"])}
        )
    return remotes


def _list_accounts(root: str) -> tuple[bool, list[dict[str, Any]]]:
    """Return ``(authenticated, accounts)`` from ``gh auth status --json hosts``.

    ``accounts`` is ``[{login, active, state, host}]``; ``authenticated`` is true
    when at least one account validates (``state == "success"``). Falls back to a
    plain ``gh auth status`` for the boolean on an older ``gh`` without ``--json``.
    """
    _, out, _ = _gh(["auth", "status", "--json", "hosts"], cwd=root)
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    hosts = data.get("hosts") if isinstance(data, dict) else None
    if isinstance(hosts, dict):
        accounts: list[dict[str, Any]] = []
        for host, entries in hosts.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("login"):
                    continue
                accounts.append(
                    {
                        "login": entry.get("login"),
                        "active": bool(entry.get("active")),
                        "state": entry.get("state"),
                        "host": entry.get("host") or host,
                    }
                )
        return any(a.get("state") == "success" for a in accounts), accounts
    # Older gh (no --json hosts): fall back to the plain status exit code.
    rc, _, _ = _gh(["auth", "status"], cwd=root)
    return rc == 0, []


def _resolved_base_nwo(root: str) -> str | None:
    """Return the gh-resolved base repo ``owner/repo`` for the checkout, or ``None``.

    Reads ``gh repo set-default --view`` — a local git-config read (no network),
    the same base ``gh`` itself resolves PRs against. ``None`` when no default is
    set (``--view`` exits non-zero) or the output isn't an ``owner/repo``.
    """
    rc, out, _ = _gh(["repo", "set-default", "--view"], cwd=root)
    if rc != 0:
        return None
    value = out.strip()
    if value and "/" in value and " " not in value:
        return value
    return None


def _gh_auth_token(root: str, login: str) -> str | None:
    """Return *login*'s GitHub token via ``gh auth token --user`` (never logged)."""
    rc, out, _ = _gh(["auth", "token", "--user", login, "-h", "github.com"], cwd=root)
    if rc != 0:
        return None
    return out.strip() or None


def _account_token_for(root: str, base_nwo: str | None = None) -> str | None:
    """Return the GH_TOKEN to run ``gh`` as this repo's preferred account, or ``None``.

    ``None`` (use ``gh``'s active auth) inside a sandbox (single broker identity),
    when no base repo resolves, or when the base has no stored account preference.

    :param root: Absolute workspace path.
    :param base_nwo: Precomputed base ``owner/repo`` to skip a repeat resolution.
    """
    if _in_sandbox():
        return None
    base = base_nwo if base_nwo is not None else _resolved_base_nwo(root)
    if not base:
        return None
    login = _config.github_account_preference(base)
    if not login:
        return None
    return _gh_auth_token(root, login)


# Cap the per-check list so a pathological rollup can't bloat the payload; the
# counts stay exact regardless.
_MAX_CHECK_RUNS = 300


def _classify_check(check: dict[str, Any]) -> str:
    """Bucket a single ``statusCheckRollup`` entry: passing / failing / pending."""
    # CheckRun carries status/conclusion; StatusContext carries state.
    state = check.get("state")
    if state is not None:
        upper = str(state).upper()
        if upper == "SUCCESS":
            return "passing"
        if upper in ("FAILURE", "ERROR"):
            return "failing"
        return "pending"
    if str(check.get("status", "")).upper() != "COMPLETED":
        return "pending"
    conclusion = str(check.get("conclusion", "")).upper()
    return "passing" if conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED") else "failing"


def _summarize_checks(rollup: Any) -> dict[str, Any]:
    """Summarize a ``statusCheckRollup`` into bucket counts + per-check details.

    :returns: ``{passing, failing, pending, total, runs}`` where ``runs`` is a
        list of ``{name, bucket, url}`` (the job names the UI shows on hover).
    """
    counts = {"passing": 0, "failing": 0, "pending": 0}
    runs: list[dict[str, Any]] = []
    if isinstance(rollup, list):
        for check in rollup:
            if not isinstance(check, dict):
                continue
            bucket = _classify_check(check)
            counts[bucket] += 1
            if len(runs) < _MAX_CHECK_RUNS:
                # CheckRun → name (falling back to the workflow); StatusContext
                # → context. Link is detailsUrl (CheckRun) or targetUrl (status).
                name = check.get("name") or check.get("context") or check.get("workflowName")
                runs.append(
                    {
                        "name": str(name) if name else "check",
                        "bucket": bucket,
                        "url": check.get("detailsUrl") or check.get("targetUrl") or None,
                    }
                )
    return {
        "passing": counts["passing"],
        "failing": counts["failing"],
        "pending": counts["pending"],
        "total": counts["passing"] + counts["failing"] + counts["pending"],
        "runs": runs,
    }


def _pr_view_json(root: str, fields: str, *, token: str | None = None) -> dict[str, Any] | None:
    """Return the branch's PR as a ``gh``-JSON object for ``fields``, or ``None``.

    A single ``gh pr view --json`` resolves the current branch's PR against the
    gh-resolved base repo as the authenticated account. With the base and account
    pinned by the panel's selectors, that one call covers fork / triangular and
    same-repo PRs alike — no head-ref heuristic — and still returns nothing for a
    base branch with no PR. ``--json`` also avoids the interactive pager and the
    Projects-classic mis-parse of a bare ``gh pr view``.

    :param token: Optional GH_TOKEN to run the call as the selected account.
    """
    rc, out, _ = _gh(["pr", "view", "--json", fields], cwd=root, token=token)
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def github_info(root: str) -> dict[str, Any]:
    """Resolve GitHub context for the workspace: repo, branch, base, and PR.

    Git-first: a git checkout is the fundamental requirement, so ``available``
    reflects "is a git repo". ``gh`` layers the repo / PR metadata on top;
    ``base_ref`` is the PR's base branch (``None`` when there's no PR, since the
    tab is a pure PR view).

    :param root: Absolute path to the session workspace.
    :returns: A ``session.github.info`` object. ``available`` is false only when
        this isn't a git repo (``reason: not_a_git_repo``). ``gh_available`` /
        ``authenticated`` report whether the ``gh`` CLI is present and signed in;
        ``repo`` / ``pr`` / ``base_ref`` are null without it. ``accounts`` /
        ``remotes`` list the selector options, with ``selected_account`` and
        ``default_remote`` the current picks (see :func:`set_github_preference`).
    """
    payload: dict[str, Any] = {"object": "session.github.info"}

    rc, out, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    if rc != 0:
        payload.update(available=False, reason="not_a_git_repo")
        return payload
    branch = out.strip()
    payload.update(
        available=True,
        branch=branch,
        base_ref=None,
        repo=None,
        pr=None,
    )

    # gh is an enhancement layer: without it (or its auth) the git diff still
    # renders; the UI notes the missing CLI / sign-in from these flags.
    if shutil.which("gh") is None:
        payload.update(gh_available=False, authenticated=False)
        return payload
    payload["gh_available"] = True

    # Enumerate the two selectors' options (all local, no network) and the
    # current base + account so the panel can render and correct the resolution.
    authenticated, accounts = _list_accounts(root)
    payload["authenticated"] = authenticated
    payload["accounts"] = accounts
    payload["remotes"] = _list_remotes(root)
    default_remote = _resolved_base_nwo(root)
    payload["default_remote"] = default_remote
    pref_login = _config.github_account_preference(default_remote) if default_remote else None
    active_login = next((a["login"] for a in accounts if a.get("active")), None)
    payload["selected_account"] = pref_login or active_login
    if not authenticated:
        return payload

    # Run the API-touching calls as the repo's preferred account (local dev only).
    token = _account_token_for(root, default_remote)

    rc, out, _ = _gh(["repo", "view", "--json", "nameWithOwner"], cwd=root, token=token)
    if rc == 0:
        try:
            data = json.loads(out)
            payload["repo"] = {"name_with_owner": data.get("nameWithOwner")}
        except (ValueError, AttributeError):
            pass

    pr: dict[str, Any] | None = None
    data = _pr_view_json(root, _PR_VIEW_FIELDS, token=token)
    if data is not None:
        author = data.get("author")
        pr = {
            "number": data.get("number"),
            "title": data.get("title"),
            "state": data.get("state"),
            "url": data.get("url"),
            "is_draft": data.get("isDraft", False),
            "author": author.get("login") if isinstance(author, dict) else None,
            "base_ref": data.get("baseRefName"),
            "head_ref": data.get("headRefName"),
            "checks": _summarize_checks(data.get("statusCheckRollup")),
        }
    payload["pr"] = pr

    # A pure PR view: the base is the PR's base branch, else null (no PR).
    payload["base_ref"] = pr.get("base_ref") if pr else None
    return payload


def set_github_preference(
    root: str,
    *,
    account: str | None = None,
    remote: str | None = None,
) -> dict[str, Any]:
    """Apply an account and/or remote selection, then return refreshed info.

    The remote is applied first (``gh repo set-default``, persisted by ``gh`` in
    ``.git/config``) so the account preference — keyed by the *new* base repo — is
    stored against the right key. The account is persisted to the user config via
    :func:`omnigent.config.set_github_account_preference`; an empty ``account``
    clears the entry, falling back to ``gh``'s active account.

    :param root: Absolute workspace path.
    :param account: GitHub login to prefer for this repo, or ``None`` to leave
        the account unchanged (empty string clears it).
    :param remote: Git remote name or ``owner/repo`` to set as the base repo, or
        ``None`` to leave the base unchanged.
    :returns: The refreshed :func:`github_info` payload.
    """
    if remote:
        _gh(["repo", "set-default", remote], cwd=root)
    if account is not None:
        base = _resolved_base_nwo(root)
        if base:
            _config.set_github_account_preference(base, account or None)
    return github_info(root)


def resolve_base_ref(root: str, base: str | None) -> str | None:
    """Return an explicit base branch, else the repo's default diff base.

    Shared by the runner routes and the host reader so both resolve an omitted
    ``?base=`` identically (via :func:`github_info`).

    :param root: Absolute workspace path.
    :param base: Explicit base branch name, or ``None`` to derive the default.
    :returns: A base branch name, or ``None`` when none can be resolved.
    """
    if base:
        return base
    return github_info(root).get("base_ref")


def _resolve_diff_base(root: str, base: str) -> str | None:
    """Resolve a base branch name to the ref to diff HEAD against.

    Prefers the merge-base of ``origin/<base>`` (or ``<base>``) and HEAD, giving
    the three-dot / "Files changed" semantics GitHub shows. Falls back to the
    base ref itself, then ``None`` when nothing resolves.

    :param root: Absolute workspace path.
    :param base: Base branch name, e.g. ``"main"``.
    :returns: A ref (SHA or name) to diff against, or ``None``.
    """
    candidates = [f"origin/{base}", base]
    resolved: str | None = None
    for candidate in candidates:
        rc, _, _ = _git(["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"], cwd=root)
        if rc == 0:
            resolved = candidate
            break
    if resolved is None:
        return None
    rc, out, _ = _git(["merge-base", resolved, "HEAD"], cwd=root)
    if rc == 0 and out.strip():
        return out.strip()
    return resolved


# GitHub pulls/files ``status`` → the status vocabulary the web list uses.
_GH_STATUS_MAP = {
    "added": "created",
    "removed": "deleted",
    "modified": "modified",
    "renamed": "renamed",
    "copied": "created",
    "changed": "modified",
    "unchanged": "modified",
}


def _pr_number(root: str, *, token: str | None = None) -> int | None:
    """Return the PR number for the workspace's branch, or ``None``.

    :param root: Absolute workspace path.
    :param token: Optional GH_TOKEN to run ``gh`` as the selected account.
    :returns: The associated PR's number, or ``None`` when no PR resolves (none
        for the branch, ``gh`` missing, or not authenticated).
    """
    data = _pr_view_json(root, "number", token=token)
    if data is None:
        return None
    number = data.get("number")
    return number if isinstance(number, int) else None


def github_changed_files(root: str) -> dict[str, Any]:
    """List the PR's changed files, straight from GitHub.

    Sourced from ``gh api .../pulls/<n>/files`` so the set (and each file's
    status / line counts) matches the PR's "Files changed" exactly — never a
    local ``git diff``. Empty when the branch has no PR.

    :param root: Absolute workspace path.
    :returns: A ``list`` object whose ``data`` entries carry ``path`` / ``name``
        / ``status`` / ``lines_added`` / ``lines_removed``.
    """
    empty: dict[str, Any] = {"object": "list", "data": [], "has_more": False}
    token = _account_token_for(root)
    number = _pr_number(root, token=token)
    if number is None:
        return empty
    # ``{owner}`` / ``{repo}`` are filled by ``gh`` from the repo; ``--paginate``
    # concatenates the pages of the (array) response into one JSON array.
    rc, out, _ = _gh(
        ["api", "--paginate", f"repos/{{owner}}/{{repo}}/pulls/{number}/files?per_page=100"],
        cwd=root,
        token=token,
    )
    if rc != 0:
        return empty
    try:
        entries = json.loads(out)
    except ValueError:
        return empty
    if not isinstance(entries, list):
        return empty

    data: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # ``filename`` is the current path (the new name for a rename) — the one
        # the diff endpoint reads at HEAD, matching the whole-PR patch.
        path = entry.get("filename")
        if not path:
            continue
        data.append(
            {
                "object": "session.github.changed_file",
                "path": path,
                "name": str(path).split("/")[-1],
                "status": _GH_STATUS_MAP.get(str(entry.get("status")), "modified"),
                "lines_added": entry.get("additions"),
                "lines_removed": entry.get("deletions"),
            }
        )
    return {"object": "list", "data": data, "has_more": False}


def github_file_diff(root: str, base: str, path: str) -> dict[str, Any]:
    """Return before/after content for one file, HEAD vs the base merge-base.

    :param root: Absolute workspace path.
    :param base: Base branch name, e.g. ``"main"``.
    :param path: Repo-root-relative path, as returned by
        :func:`github_changed_files`.
    :returns: A ``session.github.file_diff`` object with ``before`` (merge-base
        content, ``None`` for an added file) and ``after`` (HEAD content,
        ``None`` for a deleted file).
    """
    diff_base = _resolve_diff_base(root, base)

    before: str | None = None
    if diff_base is not None:
        rc, out, _ = _git(["show", f"{diff_base}:{path}"], cwd=root)
        if rc == 0:
            before = out

    after: str | None = None
    rc, out, _ = _git(["show", f"HEAD:{path}"], cwd=root)
    if rc == 0:
        after = out

    return {
        "object": "session.github.file_diff",
        "path": path,
        "before": before,
        "after": after,
    }


def github_pr_diff(root: str) -> dict[str, Any]:
    """Return the whole PR as one unified diff patch, straight from GitHub.

    ``gh pr diff <number>`` yields the PR's "Files changed" patch (server-computed
    against the base's merge-base), which the web view parses client-side into
    per-file diffs. The PR is resolved by number first (via :func:`_pr_number`,
    which handles fork / triangular heads a bare ``gh pr diff`` can't); empty when
    the branch has no PR.

    :param root: Absolute workspace path.
    :returns: A ``session.github.pr_diff`` object with the ``patch`` text
        (empty when there's no PR / no changes).
    """
    empty: dict[str, Any] = {"object": "session.github.pr_diff", "patch": ""}
    token = _account_token_for(root)
    number = _pr_number(root, token=token)
    if number is None:
        return empty
    rc, out, _ = _gh(["pr", "diff", str(number)], cwd=root, token=token)
    return {"object": "session.github.pr_diff", "patch": out if rc == 0 else ""}
