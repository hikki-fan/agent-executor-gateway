"""
Worktree Management Module for Agent Executor Gateway (Phase 8).

Implements Goal Prompt Sections 33, 35, 50:
- Isolated worktree creation inside safe root directory (<repo_parent>/.agent-worktrees/)
- Fixed branch naming convention: agent/<sanitized-task-id>-<executor>
- Strict realpath traversal prevention and containment inside worktree root
- Strict branch/conflict validation (never deletes existing branches on creation)
- Safe removal with git worktree remove and prune for manager-registered worktrees only
- Scope verification integration without automatic merging
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from orchestration.scope import ScopeCheckResult, check_scope
from orchestration.task import ALLOWED_EXECUTORS, Task

DEFAULT_WORKTREE_DIR_NAME = ".agent-worktrees"
SAFE_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


@dataclass(frozen=True)
class WorktreeInfo:
    """Metadata describing an isolated Git worktree."""
    task_id: str
    executor: str
    branch: str
    path: str
    base_commit: str
    status: str  # "active", "cleaned", "error"
    created_at: str
    repo_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorktreeInfo:
        return cls(
            task_id=str(data.get("task_id", "")),
            executor=str(data.get("executor", "agy")),
            branch=str(data.get("branch", "")),
            path=str(data.get("path", "")),
            base_commit=str(data.get("base_commit", "")),
            status=str(data.get("status", "active")),
            created_at=str(data.get("created_at", "")),
            repo_path=str(data.get("repo_path", "")),
        )


def sanitize_task_id(task_id: str) -> str:
    """
    Sanitize task_id for use in branch names and directory paths.
    Rejects path traversal tokens ('..', '/', '\\') and empty strings.
    """
    if not task_id or not isinstance(task_id, str):
        raise ValueError("task_id must be a non-empty string")

    stripped = task_id.strip()
    if not stripped:
        raise ValueError("task_id cannot be empty or whitespace")

    if "/" in stripped or "\\" in stripped or ".." in stripped:
        raise ValueError(f"task_id '{task_id}' contains forbidden path traversal characters")

    cleaned = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", stripped)
    if not SAFE_ID_REGEX.match(cleaned):
        raise ValueError(f"task_id '{task_id}' could not be sanitized safely")

    return cleaned


def get_default_worktree_root(repo_path: str) -> str:
    """Return default worktree root in repository parent directory: <repo_parent>/.agent-worktrees/"""
    abs_repo = os.path.realpath(repo_path)
    parent_dir = os.path.dirname(abs_repo)
    return os.path.join(parent_dir, DEFAULT_WORKTREE_DIR_NAME)


def is_path_inside_root(target_path: str, root_dir: str) -> bool:
    """Verify that target_path resides strictly within root_dir using realpath without traversal or symlink escape."""
    real_root = os.path.realpath(root_dir)
    real_target = os.path.realpath(target_path)
    try:
        common = os.path.commonpath([real_root, real_target])
        return common == real_root and real_target != real_root
    except ValueError:
        return False


class WorktreeManager:
    """Manages creation, inspection, and safe removal of isolated Git worktrees."""

    def __init__(self, repo_path: str, root_dir: str | None = None) -> None:
        self.repo_path = os.path.realpath(repo_path)
        if not os.path.exists(self.repo_path):
            raise FileNotFoundError(f"Repository path does not exist: {self.repo_path}")

        raw_root = root_dir if root_dir else get_default_worktree_root(self.repo_path)
        self.root_dir = os.path.realpath(raw_root)

        # Validate that root_dir is NOT the repository itself or inside the repository
        real_repo = self.repo_path
        real_root = self.root_dir
        if real_root == real_repo:
            raise ValueError(f"Worktree root directory cannot be the main repository itself: '{self.root_dir}'")
        try:
            is_inside = (os.path.commonpath([real_repo, real_root]) == real_repo)
        except ValueError:
            is_inside = False

        if is_inside:
            raise ValueError(f"Worktree root directory cannot reside inside the main repository: '{self.root_dir}'")

    def _run_git(self, args: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        target_cwd = cwd or self.repo_path
        return subprocess.run(
            ["git"] + args,
            cwd=target_cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=check,
        )

    def create_worktree(
        self,
        task: Task,
        executor: str | None = None,
    ) -> WorktreeInfo:
        """
        Create a new branch and worktree at base_commit for the given Task.

        Branch: agent/<sanitized-task-id>-<executor>
        Path:   <root_dir>/<sanitized-task-id>-<executor>
        """
        # 1. Validate repository path alignment
        task_repo_real = os.path.realpath(task.repository.path)
        if task_repo_real != self.repo_path:
            raise ValueError(
                f"Task repository path '{task.repository.path}' (resolved: '{task_repo_real}') does not match WorktreeManager repository '{self.repo_path}'"
            )

        clean_task_id = sanitize_task_id(task.task_id)
        effective_executor = (executor or task.execution.executor).strip().lower()
        if effective_executor not in ALLOWED_EXECUTORS:
            raise ValueError(f"Invalid executor '{effective_executor}'. Must be one of {list(ALLOWED_EXECUTORS)}")

        branch_name = f"agent/{clean_task_id}-{effective_executor}"
        folder_name = f"{clean_task_id}-{effective_executor}"
        target_path = os.path.realpath(os.path.join(self.root_dir, folder_name))

        if not is_path_inside_root(target_path, self.root_dir):
            raise ValueError(f"Worktree path '{target_path}' escapes designated root directory '{self.root_dir}'")

        if os.path.exists(target_path):
            raise FileExistsError(f"Worktree destination directory already exists: '{target_path}'")

        # 2. Check if branch already exists in repository -> Reject conflict (destructive branch -D forbidden)
        proc_check_branch = self._run_git(["rev-parse", "--verify", f"refs/heads/{branch_name}"], check=False)
        if proc_check_branch.returncode == 0:
            raise FileExistsError(
                f"Branch '{branch_name}' already exists in repository. Recreating or overwriting existing branches is forbidden."
            )

        # 3. Check if existing worktree is already registered
        for wt in self.list_worktrees():
            if wt.task_id == task.task_id or wt.branch == branch_name or os.path.realpath(wt.path) == target_path:
                raise FileExistsError(f"Worktree for task '{task.task_id}' or branch '{branch_name}' already exists.")

        os.makedirs(self.root_dir, exist_ok=True)

        base_commit = task.repository.base_commit
        if not base_commit:
            raise ValueError("Task repository.base_commit must be specified to create isolated worktree")

        res = self._run_git(
            ["worktree", "add", "-b", branch_name, target_path, base_commit],
            check=False,
        )
        if res.returncode != 0:
            err_msg = res.stderr.strip() or res.stdout.strip()
            raise RuntimeError(f"Failed to create git worktree '{target_path}' at {base_commit}: {err_msg}")

        timestamp = datetime.now(timezone.utc).isoformat()
        return WorktreeInfo(
            task_id=task.task_id,
            executor=effective_executor,
            branch=branch_name,
            path=target_path,
            base_commit=base_commit,
            status="active",
            created_at=timestamp,
            repo_path=self.repo_path,
        )

    def list_worktrees(self) -> list[WorktreeInfo]:
        """
        List all active worktrees managed under this root_dir with branch starting with agent/.
        Arbitrary or external worktrees are excluded.
        """
        res = self._run_git(["worktree", "list", "--porcelain"], check=False)
        if res.returncode != 0:
            return []

        worktrees: list[WorktreeInfo] = []
        current_wt: dict[str, str] = {}

        def _process_entry(entry: dict[str, str]) -> None:
            if "worktree" not in entry:
                return
            wt_path = os.path.realpath(entry["worktree"])
            if is_path_inside_root(wt_path, self.root_dir):
                branch = entry.get("branch", "")
                if branch.startswith("refs/heads/"):
                    branch = branch[len("refs/heads/"):]

                # Only include worktrees on agent/ branches
                if branch.startswith("agent/"):
                    head_commit = entry.get("HEAD", "")
                    folder_name = os.path.basename(wt_path)
                    parts = folder_name.rsplit("-", 1)
                    task_id = parts[0] if parts else folder_name
                    executor = parts[1] if len(parts) > 1 and parts[1] in ALLOWED_EXECUTORS else "unknown"

                    worktrees.append(
                        WorktreeInfo(
                            task_id=task_id,
                            executor=executor,
                            branch=branch,
                            path=wt_path,
                            base_commit=head_commit,
                            status="active",
                            created_at="",
                            repo_path=self.repo_path,
                        )
                    )

        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                _process_entry(current_wt)
                current_wt = {}
            elif line.startswith("worktree "):
                current_wt["worktree"] = line[len("worktree "):].strip()
            elif line.startswith("HEAD "):
                current_wt["HEAD"] = line[len("HEAD "):].strip()
            elif line.startswith("branch "):
                current_wt["branch"] = line[len("branch "):].strip()

        _process_entry(current_wt)
        return worktrees

    def cleanup_worktree(
        self,
        task_id_or_path: str,
        force: bool = True,
        delete_branch: bool = True,
    ) -> bool:
        """
        Safely remove a manager-registered worktree and its branch.
        Rejects non-registered directories, arbitrary files, or non-agent worktrees.
        """
        # Strict containment check if an absolute/relative path was passed
        if os.path.isabs(task_id_or_path) or os.path.exists(task_id_or_path):
            if not is_path_inside_root(task_id_or_path, self.root_dir):
                raise ValueError(f"Security error: target path '{task_id_or_path}' is outside root directory '{self.root_dir}'")

        # Find among registered agent worktrees ONLY
        active = self.list_worktrees()
        matched_wt: WorktreeInfo | None = None
        for wt in active:
            if (
                wt.task_id == task_id_or_path
                or os.path.realpath(wt.path) == os.path.realpath(task_id_or_path)
                or os.path.basename(wt.path) == task_id_or_path
            ):
                matched_wt = wt
                break

        if not matched_wt:
            # Unregistered / untrusted / non-agent object; refuse to delete
            return False

        target_path = matched_wt.path
        branch_name = matched_wt.branch

        # Enforce containment safety
        if not is_path_inside_root(target_path, self.root_dir):
            raise ValueError(f"Security error: target path '{target_path}' is outside root directory '{self.root_dir}'")

        if not branch_name.startswith("agent/"):
            raise ValueError(f"Security error: cannot cleanup non-agent branch '{branch_name}'")

        # Remove git worktree
        cmd = ["worktree", "remove"]
        if force:
            cmd.append("--force")
        cmd.append(target_path)
        res = self._run_git(cmd, check=False)
        if res.returncode != 0:
            # Never fall back to deleting a directory when Git refused the
            # removal (for example because the worktree is dirty or stale).
            # The caller can inspect Git's diagnostics and decide whether a
            # later explicit force cleanup is appropriate.
            return False
        self._run_git(["worktree", "prune"], check=False)

        # Check if worktree is still listed in Git
        remaining_paths = {os.path.realpath(w.path) for w in self.list_worktrees()}
        if os.path.realpath(target_path) in remaining_paths:
            # git worktree remove failed; refuse to rmtree directory
            return False

        # Remove leftover directory only after confirmed git worktree removal (never follow symlinks)
        if os.path.exists(target_path) and is_path_inside_root(target_path, self.root_dir) and not os.path.islink(target_path):
            shutil.rmtree(target_path, ignore_errors=True)

        if delete_branch and branch_name.startswith("agent/"):
            self._run_git(["branch", "-D", branch_name], check=False)

        return True

    def check_worktree_scope(self, task: Task, worktree: WorktreeInfo) -> ScopeCheckResult:
        """Check scope boundaries directly within the isolated worktree directory."""
        return check_scope(
            repo_path=worktree.path,
            allowed_paths=task.scope.allowed_paths,
            forbidden_paths=task.scope.forbidden_paths,
            base_commit=task.repository.base_commit,
        )
