"""
Scope Control Module for Agent Executor Gateway (Phase 6).

Implements Goal Prompt Section 29:
- Extract all modified, staged, committed (against base_commit), and untracked files via Git.
- Match changed files against allowed_paths and forbidden_paths glob patterns.
- Return verification failure with reason='scope_violation' when boundaries are exceeded.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class ScopeCheckResult:
    """Result of scope boundary verification."""
    passed: bool
    changed_files: list[str] = field(default_factory=list)
    violating_files: list[str] = field(default_factory=list)
    reason: str | None = None
    details: str = ""

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "changed_files": self.changed_files,
            "violating_files": self.violating_files,
            "reason": self.reason,
            "details": self.details,
        }


def normalize_relpath(path: str) -> str:
    """Normalize file path to unix forward slashes without leading ./ or /."""
    p = path.replace("\\", "/").strip()
    if p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    return p


def match_glob_pattern(file_path: str, pattern: str) -> bool:
    """
    Match a relative file path against a glob pattern supporting `**`, `*`, and `?`.

    Examples:
    - 'backend/**' matches 'backend/foo.py' and 'backend/tasks/worker.py'
    - 'backend/*.py' matches 'backend/foo.py' but not 'backend/tasks/worker.py'
    - 'database/migrations/**' matches 'database/migrations/001.sql'
    - '*.py' matches 'main.py'
    - '**/*.py' matches 'main.py' and 'sub/main.py'
    """
    f = normalize_relpath(file_path)
    p = normalize_relpath(pattern)

    if not p:
        return False

    # Exact match
    if f == p:
        return True

    if p == "**":
        return True

    # Trailing /** matches directory and all descendants
    if p.endswith("/**"):
        prefix = p[:-3]
        if f == prefix or f.startswith(prefix + "/"):
            return True

    # Leading **/ matches at any depth
    if p.startswith("**/"):
        suffix = p[3:]
        if match_glob_pattern(os.path.basename(f), suffix):
            return True

    # Convert glob pattern to regex:
    # ** matches any characters across /
    # * matches any characters except /
    # ? matches single non-slash character
    parts = p.split("**")
    regex_parts = []
    for part in parts:
        escaped = ""
        for ch in part:
            if ch == "*":
                escaped += "[^/]*"
            elif ch == "?":
                escaped += "[^/]"
            else:
                escaped += re.escape(ch)
        regex_parts.append(escaped)

    regex_str = "^" + ".*".join(regex_parts) + "$"
    try:
        if re.match(regex_str, f):
            return True
    except re.error:
        pass

    return False


def _is_internal_ignored_path(path: str) -> bool:
    """Check if a path is internal orchestration/git metadata and should be ignored from scope."""
    p = normalize_relpath(path)
    return (
        p == ".git"
        or p.startswith(".git/")
        or p == ".agent"
        or p.startswith(".agent/")
    )


def get_git_changed_and_untracked_files(
    repo_path: str, base_commit: str | None = None
) -> list[str]:
    """
    Query Git to retrieve all changed files (committed diff against base_commit,
    unstaged changes, staged changes) and all untracked files, ignoring internal .agent/ and .git/ directories.
    """
    if not os.path.exists(repo_path):
        return []

    changed: set[str] = set()

    def _run_git(args: list[str]) -> str:
        try:
            res = subprocess.run(
                ["git"] + args,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if res.returncode == 0:
                return res.stdout
            return ""
        except Exception:
            return ""

    # 1. Diff against base_commit (if provided)
    if base_commit:
        diff_out = _run_git(["diff", "--name-only", f"{base_commit}..HEAD"])
        if not diff_out:
            # Fallback to direct diff with base_commit
            diff_out = _run_git(["diff", "--name-only", base_commit])
        for line in diff_out.splitlines():
            line = line.strip()
            if line and not _is_internal_ignored_path(line):
                changed.add(normalize_relpath(line))

    # 2. Unstaged working tree changes
    unstaged_out = _run_git(["diff", "--name-only"])
    for line in unstaged_out.splitlines():
        line = line.strip()
        if line and not _is_internal_ignored_path(line):
            changed.add(normalize_relpath(line))

    # 3. Staged index changes
    staged_out = _run_git(["diff", "--name-only", "--cached"])
    for line in staged_out.splitlines():
        line = line.strip()
        if line and not _is_internal_ignored_path(line):
            changed.add(normalize_relpath(line))

    # 4. Untracked files and untracked status
    status_out = _run_git(["status", "--porcelain", "-uall"])
    for line in status_out.splitlines():
        if len(line) >= 3:
            path_part = line[3:].strip()
            if " -> " in path_part:
                # Renamed file
                path_part = path_part.split(" -> ")[1].strip()
            if path_part and not _is_internal_ignored_path(path_part):
                changed.add(normalize_relpath(path_part))

    return sorted(list(changed))


def _base_commit_exists(repo_path: str, base_commit: str) -> bool:
    """Return whether base_commit resolves to a commit in repo_path."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{base_commit}^{{commit}}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def check_scope(
    repo_path: str,
    allowed_paths: Sequence[str] = (),
    forbidden_paths: Sequence[str] = (),
    base_commit: str | None = None,
    explicit_changed_files: Sequence[str] | None = None,
) -> ScopeCheckResult:
    """
    Check modified/untracked files against allowed and forbidden patterns.

    Enforces:
    - If allowed_paths is non-empty: every changed file must match at least one pattern in allowed_paths.
    - If forbidden_paths is non-empty: no changed file can match any pattern in forbidden_paths.
    - If any violation occurs, returns passed=False with reason='scope_violation'.
    """
    if explicit_changed_files is not None:
        changed_files = [normalize_relpath(f) for f in explicit_changed_files if f.strip()]
    else:
        if base_commit and not _base_commit_exists(repo_path, base_commit):
            return ScopeCheckResult(
                passed=False,
                changed_files=[],
                violating_files=[],
                reason="invalid_base_commit",
                details=f"Base commit does not resolve in repository: {base_commit}",
            )
        changed_files = get_git_changed_and_untracked_files(repo_path, base_commit=base_commit)

    violating_files: list[str] = []
    violation_reasons: list[str] = []

    for f in changed_files:
        # Check forbidden_paths
        is_forbidden = False
        for forbidden_pattern in forbidden_paths:
            if match_glob_pattern(f, forbidden_pattern):
                violating_files.append(f)
                violation_reasons.append(f"File '{f}' matches forbidden pattern '{forbidden_pattern}'")
                is_forbidden = True
                break

        if is_forbidden:
            continue

        # Check allowed_paths (if defined and non-empty)
        if allowed_paths:
            is_allowed = any(match_glob_pattern(f, allowed_pat) for allowed_pat in allowed_paths)
            if not is_allowed:
                violating_files.append(f)
                violation_reasons.append(f"File '{f}' is not in allowed_paths {list(allowed_paths)}")

    if violating_files:
        details = f"Scope violation detected: {'; '.join(violation_reasons)}"
        return ScopeCheckResult(
            passed=False,
            changed_files=changed_files,
            violating_files=sorted(list(set(violating_files))),
            reason="scope_violation",
            details=details,
        )

    return ScopeCheckResult(
        passed=True,
        changed_files=changed_files,
        violating_files=[],
        reason=None,
        details="All changed files are within permitted scope",
    )
