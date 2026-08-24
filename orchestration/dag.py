"""
Task DAG and Parallel Execution Infrastructure for Agent Executor Gateway (Phase 8).

Implements Goal Prompt Sections 32, 34, 36, 37, 50:
- Deterministic DAG validation, dependency tracking, and cycle detection
- Strict readiness evaluation: Task is READY only when all depends_on are DONE
- BLOCKED propagation when dependencies fail or are cancelled
- Bounded parallel dispatch for independent tasks (e.g. AGY Task-A + Grok Task-B in separate worktrees)
- Prohibits automatic merge/cherry-pick; preserves worktrees/commits for Codex review
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from orchestration.task import Task
from orchestration.worktree import WorktreeInfo, WorktreeManager

# Task Lifecycle States (Section 36)
STATE_CREATED = "CREATED"
STATE_READY = "READY"
STATE_RUNNING = "RUNNING"
STATE_VERIFYING = "VERIFYING"
STATE_REVIEWING = "REVIEWING"
STATE_INTEGRATING = "INTEGRATING"
STATE_DONE = "DONE"
STATE_FAILED = "FAILED"
STATE_BLOCKED = "BLOCKED"
STATE_CANCELLED = "CANCELLED"

TERMINAL_STATES = {STATE_DONE, STATE_FAILED, STATE_BLOCKED, STATE_CANCELLED}
ACTIVE_STATES = {STATE_RUNNING, STATE_VERIFYING, STATE_REVIEWING, STATE_INTEGRATING}


@dataclass
class TaskNode:
    """Represents a node in the Task DAG with its execution metadata."""
    task: Task
    status: str = STATE_CREATED
    worktree: WorktreeInfo | None = None
    result: Any = None
    error: str | None = None
    attempts: int = 0
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task.task_id,
            "goal": self.task.goal,
            "status": self.status,
            "depends_on": list(self.task.depends_on),
            "executor": self.task.execution.executor,
            "complexity": self.task.classification.complexity,
            "risk": self.task.classification.risk,
            "worktree": self.worktree.to_dict() if self.worktree else None,
            "error": self.error,
            "attempts": self.attempts,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class TaskDAG:
    """Manages a collection of interdependent Tasks and evaluates execution readiness."""

    def __init__(self, tasks: Iterable[Task] | None = None) -> None:
        self.nodes: dict[str, TaskNode] = {}
        self._lock = threading.Lock()
        if tasks:
            for t in tasks:
                self.add_task(t)

    def add_task(self, task: Task) -> None:
        """Add a task to the DAG."""
        with self._lock:
            if task.task_id in self.nodes:
                raise ValueError(f"Duplicate task_id in DAG: '{task.task_id}'")
            self.nodes[task.task_id] = TaskNode(task=task, status=STATE_CREATED)

    def get_task(self, task_id: str) -> TaskNode | None:
        """Retrieve task node by task_id."""
        with self._lock:
            return self.nodes.get(task_id)

    def validate_dag(self) -> tuple[bool, list[str]]:
        """
        Validate DAG topology:
        1. All depends_on references must exist in the DAG.
        2. No cyclic dependencies exist.
        """
        with self._lock:
            errors: list[str] = []

            # 1. Missing dependency check
            for task_id, node in self.nodes.items():
                for dep in node.task.depends_on:
                    if dep not in self.nodes:
                        errors.append(f"Task '{task_id}' depends on unknown task '{dep}'")

            if errors:
                return False, errors

            # 2. Cycle detection via Kahn's algorithm (in-degree calculation)
            in_degree: dict[str, int] = {tid: len(node.task.depends_on) for tid, node in self.nodes.items()}
            queue = [tid for tid, deg in in_degree.items() if deg == 0]
            visited_count = 0

            # Adjacency list: dep -> dependents
            dependents: dict[str, list[str]] = {tid: [] for tid in self.nodes}
            for tid, node in self.nodes.items():
                for dep in node.task.depends_on:
                    dependents[dep].append(tid)

            while queue:
                curr = queue.pop(0)
                visited_count += 1
                for dep_task in dependents.get(curr, []):
                    in_degree[dep_task] -= 1
                    if in_degree[dep_task] == 0:
                        queue.append(dep_task)

            if visited_count != len(self.nodes):
                errors.append("Circular dependency (cycle) detected in Task DAG")
                return False, errors

            return True, []

    def evaluate_readiness(self) -> dict[str, str]:
        """
        Evaluate and update readiness state for all non-terminal task nodes.
        - Ready when all depends_on are DONE.
        - Blocked when any depends_on is FAILED or CANCELLED.
        """
        with self._lock:
            state_map: dict[str, str] = {}
            for task_id, node in self.nodes.items():
                if node.status in TERMINAL_STATES or node.status in ACTIVE_STATES:
                    state_map[task_id] = node.status
                    continue

                # Check dependencies
                deps = node.task.depends_on
                if not deps:
                    node.status = STATE_READY
                else:
                    missing_deps = [d for d in deps if d not in self.nodes]
                    if missing_deps:
                        node.status = STATE_BLOCKED
                    else:
                        dep_statuses = [self.nodes[d].status for d in deps]
                        if any(s in (STATE_FAILED, STATE_CANCELLED, STATE_BLOCKED) for s in dep_statuses):
                            node.status = STATE_BLOCKED
                        elif all(s == STATE_DONE for s in dep_statuses):
                            node.status = STATE_READY
                        else:
                            node.status = STATE_CREATED

                state_map[task_id] = node.status
            return state_map

    def get_ready_tasks(self) -> list[TaskNode]:
        """Return list of TaskNodes currently in READY status."""
        self.evaluate_readiness()
        with self._lock:
            return [node for node in self.nodes.values() if node.status == STATE_READY]

    def update_status(
        self,
        task_id: str,
        new_status: str,
        result: Any = None,
        error: str | None = None,
        worktree: WorktreeInfo | None = None,
    ) -> None:
        """Update node status and associated metadata."""
        with self._lock:
            node = self.nodes.get(task_id)
            if not node:
                raise KeyError(f"Task '{task_id}' not found in DAG")
            node.status = new_status
            if result is not None:
                node.result = result
            if error is not None:
                node.error = error
            if worktree is not None:
                node.worktree = worktree

            timestamp = datetime.now(timezone.utc).isoformat()
            if new_status in ACTIVE_STATES and not node.started_at:
                node.started_at = timestamp
            if new_status in TERMINAL_STATES:
                node.finished_at = timestamp

    def is_completed(self) -> bool:
        """Return True if all tasks in the DAG are in a terminal state."""
        with self._lock:
            return all(node.status in TERMINAL_STATES for node in self.nodes.values())

    def to_dict(self) -> dict[str, Any]:
        """Serialize complete DAG status to dictionary."""
        with self._lock:
            return {
                "tasks": {tid: node.to_dict() for tid, node in self.nodes.items()},
                "completed": all(node.status in TERMINAL_STATES for node in self.nodes.values()),
            }


def run_dag_parallel(
    dag: TaskDAG,
    worker_fn: Callable[[Task, WorktreeInfo], Any],
    repo_path: str,
    worktree_root: str | None = None,
    max_concurrency: int = 2,
    cleanup_on_success: bool = False,
) -> dict[str, Any]:
    """
    Execute a TaskDAG in parallel with bounded concurrency across isolated worktrees.

    Guarantees:
    - Never merges or cherry-picks into main branch automatically.
    - Each task runs inside its own isolated Git worktree.
    - Prevents dispatching the same task multiple times.
    """
    valid, errors = dag.validate_dag()
    if not valid:
        raise ValueError(f"Invalid Task DAG: {'; '.join(errors)}")

    wt_manager = WorktreeManager(repo_path=repo_path, root_dir=worktree_root)
    dispatched: set[str] = set()
    active_futures: dict[concurrent.futures.Future, str] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        while not dag.is_completed():
            # Find newly ready tasks
            ready_nodes = dag.get_ready_tasks()
            for node in ready_nodes:
                tid = node.task.task_id
                if tid not in dispatched:
                    dispatched.add(tid)
                    dag.update_status(tid, STATE_RUNNING)

                    # Create worktree for task
                    wt_info = wt_manager.create_worktree(node.task)
                    dag.update_status(tid, STATE_RUNNING, worktree=wt_info)

                    # Submit to threadpool
                    fut = executor.submit(worker_fn, node.task, wt_info)
                    active_futures[fut] = tid

            if not active_futures:
                # No active tasks and nothing ready; remaining are blocked
                dag.evaluate_readiness()
                if not any(n.status == STATE_READY for n in dag.nodes.values()):
                    break

            # Wait for at least one future to complete
            done_futures, _ = concurrent.futures.wait(
                active_futures.keys(),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )

            for fut in done_futures:
                tid = active_futures.pop(fut)
                node = dag.get_task(tid)
                try:
                    res = fut.result()
                    # Determine task outcome
                    # If worker returned a dict with 'status' == 'failed' or boolean False
                    if isinstance(res, dict) and res.get("status") in ("failed", "error"):
                        dag.update_status(tid, STATE_FAILED, result=res, error=res.get("error", "Task failed"))
                    elif res is False:
                        dag.update_status(tid, STATE_FAILED, result=res, error="Worker returned failure")
                    else:
                        dag.update_status(tid, STATE_DONE, result=res)
                        if cleanup_on_success and node and node.worktree:
                            wt_manager.cleanup_worktree(node.worktree.path)
                except Exception as e:
                    dag.update_status(tid, STATE_FAILED, error=str(e))

            # Re-evaluate DAG readiness
            dag.evaluate_readiness()

    return dag.to_dict()
