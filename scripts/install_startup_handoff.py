#!/usr/bin/env python3
"""Install the Agent Executor Gateway startup handoff.

The command is intentionally read-only unless all of the following are supplied:

* ``--apply``;
* ``--confirm-startup-handoff``; and
* ``CONFIRM_STARTUP_HANDOFF=1``.

The default ``--check`` mode validates and renders the proposed changes without
creating files, changing modes, or touching the running production service.  An
authorized apply creates a private backup first, writes both startup files
atomically, and restores the first file if the second write fails.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


MARKER = "AGENT_EXECUTOR_GATEWAY_STARTUP_HANDOFF"
DEFAULT_ENTRYPOINT = "/usr/local/bin/start-codex-container"
DEFAULT_PROFILE = str(Path(os.environ.get("HOME", "/home/codex")) / ".bashrc")
DEFAULT_TOKEN = "/home/codex/.codex/acp_token"
DEFAULT_RUNTIME = "/home/codex/.agent-executor-gateway/production"


class HandoffError(RuntimeError):
    """Raised when the handoff cannot be safely inspected or rendered."""


@dataclass(frozen=True)
class Target:
    path: Path
    label: str


def shell_path(path: Path) -> str:
    return shlex.quote(str(path))


def secure_target(target: Target) -> os.stat_result:
    """Validate a startup target without following a symbolic link."""

    try:
        info = target.path.lstat()
    except FileNotFoundError as exc:
        raise HandoffError(f"{target.label} does not exist: {target.path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise HandoffError(f"{target.label} is a symbolic link: {target.path}")
    if not stat.S_ISREG(info.st_mode):
        raise HandoffError(f"{target.label} is not a regular file: {target.path}")
    if not os.access(target.path, os.R_OK):
        raise HandoffError(f"{target.label} is not readable: {target.path}")
    # Do not let an unrelated owner trick an authorized invocation into
    # replacing a file the operator does not control.  Root may repair files
    # owned by the container user; an unprivileged caller must own the target.
    if os.geteuid() != 0 and info.st_uid != os.geteuid():
        raise HandoffError(f"{target.label} is not owned by the current user: {target.path}")
    return info


def entrypoint_block(cli: Path, watchdog: Path, token: Path, runtime: Path) -> str:
    return (
        "# AGENT_EXECUTOR_GATEWAY_STARTUP_HANDOFF\n"
        f"if [ -x {shell_path(cli)} ]; then\n"
        f"  ln -sfn {shell_path(cli)} /usr/local/bin/acp-cli\n"
        "fi\n"
        f"if [ -x {shell_path(watchdog)} ]; then\n"
        f"  ACP_PORT=8765 \\\n"
        f"  ACP_TOKEN_FILE={shell_path(token)} \\\n"
        f"  GATEWAY_RUN_DIR={shell_path(runtime)} \\\n"
        f"    setsid bash {shell_path(watchdog)} </dev/null >/dev/null 2>&1 &\n"
        "fi\n\n"
    )


def profile_block(watchdog: Path, token: Path, runtime: Path) -> str:
    # The profile deliberately does not rewrite /usr/local/bin/acp-cli: a
    # normal codex user may not have permission to modify that system path.
    return (
        f"# {MARKER}\n"
        f"if [ -x {shell_path(watchdog)} ]; then\n"
        f"  ACP_PORT=8765 \\\n"
        f"  ACP_TOKEN_FILE={shell_path(token)} \\\n"
        f"  GATEWAY_RUN_DIR={shell_path(runtime)} \\\n"
        f"    setsid bash {shell_path(watchdog)} </dev/null >/dev/null 2>&1 &\n"
        "fi\n\n"
    )


def _already_installed(text: str, cli: Path, watchdog: Path) -> bool:
    return (
        MARKER in text
        and str(watchdog) in text
        and (str(cli) in text or "acp-cli" not in text)
        and "/workspace/scripts/acp_watchdog.sh" not in text
        and "/workspace/scripts/ensure_acp_bridge.sh" not in text
        and "/workspace/scripts/acp-cli" not in text
        and "/workspace/antigravity-rest-bridge/acp-cli" not in text
    )


def render_entrypoint(text: str, cli: Path, watchdog: Path, token: Path, runtime: Path) -> str:
    if _already_installed(text, cli, watchdog):
        return text

    # Fail closed if the file has drifted from the known legacy layout.  This
    # prevents a broad regex from deleting unrelated container initialization.
    cli_re = re.compile(r"(?ms)^# Restore the ACP CLI.*?^fi\n\n")
    watchdog_re = re.compile(r"(?ms)^# Start the health watchdog.*?^fi\n\n")
    cli_match = cli_re.search(text)
    watchdog_match = watchdog_re.search(text)
    if not cli_match or not watchdog_match:
        raise HandoffError(
            "container entrypoint does not match the known legacy ACP handoff; "
            "review it manually before installation"
        )
    legacy_region = cli_match.group(0) + watchdog_match.group(0)
    if not any(
        marker in legacy_region
        for marker in (
            "/workspace/scripts/acp-cli",
            "/workspace/antigravity-rest-bridge/acp-cli",
            "acp_watchdog.sh",
        )
    ):
        raise HandoffError("legacy entrypoint blocks were not positively identified")

    replacements = [
        (cli_match.start(), cli_match.end(), entrypoint_block(cli, watchdog, token, runtime)),
        (watchdog_match.start(), watchdog_match.end(), ""),
    ]
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]

    if MARKER not in text or str(watchdog) not in text:
        raise HandoffError("rendered container entrypoint is missing the new handoff marker")
    if any(
        legacy in text
        for legacy in (
            "acp_watchdog.sh",
            "ensure_acp_bridge.sh",
            "/workspace/scripts/acp-cli",
            "/workspace/antigravity-rest-bridge/acp-cli",
        )
    ):
        raise HandoffError("rendered container entrypoint still contains a legacy hook")
    return text


def render_profile(text: str, watchdog: Path, token: Path, runtime: Path) -> str:
    if _already_installed(text, Path("/workspace/agent-executor-gateway/acp-cli"), watchdog):
        return text

    legacy_re = re.compile(
        r"(?m)^[ \t]*/workspace/scripts/ensure_acp_bridge\.sh[ \t]*>/dev/null[ \t]*2>&1[ \t]*\n?"
    )
    if not legacy_re.search(text):
        if "ensure_acp_bridge.sh" in text or "acp_watchdog.sh" in text:
            raise HandoffError("shell profile contains an unrecognized legacy bridge hook")
        # A profile with no legacy hook can be safely extended with the new
        # marker, which is useful for a newly provisioned container.
        return text.rstrip("\n") + "\n\n" + profile_block(watchdog, token, runtime)

    text = legacy_re.sub(profile_block(watchdog, token, runtime), text, count=1)
    if "ensure_acp_bridge.sh" in text or "acp_watchdog.sh" in text:
        raise HandoffError("rendered shell profile still contains a legacy hook")
    return text


def atomic_write(path: Path, content: str, info: os.stat_result) -> None:
    """Replace ``path`` atomically while retaining ownership and safe mode."""

    mode = stat.S_IMODE(info.st_mode) & ~0o022
    if mode == 0:
        mode = 0o600
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.handoff-", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        if os.geteuid() == 0:
            os.fchown(fd, info.st_uid, info.st_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def backup_target(target: Target, backup_dir: Path, info: os.stat_result) -> Path:
    backup_path = backup_dir / target.path.name
    shutil.copyfile(target.path, backup_path)
    os.chmod(backup_path, 0o600)
    if os.geteuid() == 0:
        os.chown(backup_path, info.st_uid, info.st_gid)
    return backup_path


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate and preview changes (default)")
    parser.add_argument("--apply", action="store_true", help="apply changes; requires both confirmations")
    parser.add_argument("--confirm-startup-handoff", action="store_true")
    parser.add_argument("--entrypoint", default=DEFAULT_ENTRYPOINT)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--gateway-cli", default=str(repo_root / "acp-cli"))
    parser.add_argument("--gateway-watchdog", default=str(repo_root / "scripts" / "gateway_watchdog.sh"))
    parser.add_argument("--token-file", default=DEFAULT_TOKEN)
    parser.add_argument("--runtime-dir", default=DEFAULT_RUNTIME)
    parser.add_argument("--backup-root", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.apply and not args.confirm_startup_handoff:
        raise HandoffError("--apply requires --confirm-startup-handoff")
    if args.apply and os.environ.get("CONFIRM_STARTUP_HANDOFF") != "1":
        raise HandoffError("--apply requires CONFIRM_STARTUP_HANDOFF=1")
    if args.check and args.apply:
        raise HandoffError("choose only one of --check or --apply")

    entrypoint = Target(Path(args.entrypoint), "Container entrypoint")
    profile = Target(Path(args.profile), "Shell profile")
    entry_info = secure_target(entrypoint)
    profile_info = secure_target(profile)
    cli = Path(args.gateway_cli).resolve()
    watchdog = Path(args.gateway_watchdog).resolve()
    token = Path(args.token_file)
    runtime = Path(args.runtime_dir)
    if not cli.is_file() or cli.is_symlink() or not os.access(cli, os.X_OK):
        raise HandoffError(f"new gateway client must be a regular executable: {cli}")
    if not watchdog.is_file() or watchdog.is_symlink() or not os.access(watchdog, os.X_OK):
        raise HandoffError(f"new gateway watchdog must be a regular executable: {watchdog}")
    if token.is_symlink():
        raise HandoffError(f"gateway token file is a symbolic link: {token}")
    if runtime.is_symlink():
        raise HandoffError(f"gateway runtime directory is a symbolic link: {runtime}")

    entry_text = entrypoint.path.read_text(encoding="utf-8")
    profile_text = profile.path.read_text(encoding="utf-8")
    new_entry = render_entrypoint(entry_text, cli, watchdog, token, runtime)
    new_profile = render_profile(profile_text, watchdog, token, runtime)

    print("Startup handoff inspection: PASS")
    print(f"  entrypoint: {entrypoint.path} ({'changed' if new_entry != entry_text else 'already installed'})")
    print(f"  profile:    {profile.path} ({'changed' if new_profile != profile_text else 'already installed'})")
    print(f"  watchdog:   {watchdog}")
    print(f"  client:     {cli}")
    if not args.apply:
        print("Dry run only: no startup file, mode, backup, or production state was changed.")
        return 0

    backup_root = Path(args.backup_root) if args.backup_root else runtime / "startup-backups"
    if backup_root.exists() and backup_root.is_symlink():
        raise HandoffError(f"backup root is a symbolic link: {backup_root}")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = backup_root / timestamp
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(backup_dir, 0o700)
    backups: list[tuple[Target, Path, os.stat_result]] = []
    try:
        for target, info in ((entrypoint, entry_info), (profile, profile_info)):
            backup_path = backup_target(target, backup_dir, info)
            backups.append((target, backup_path, info))
        manifest = {
            "marker": MARKER,
            "entrypoint": str(entrypoint.path),
            "profile": str(profile.path),
            "gateway_cli": str(cli),
            "gateway_watchdog": str(watchdog),
            "token_file": str(token),
            "runtime_dir": str(runtime),
            "backup_dir": str(backup_dir),
        }
        (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.chmod(backup_dir / "manifest.json", 0o600)
        atomic_write(entrypoint.path, new_entry, entry_info)
        atomic_write(profile.path, new_profile, profile_info)
    except Exception:
        # Restore any target that was already replaced.  The backup itself is
        # retained for forensic review even when an apply fails.
        for target, backup_path, info in reversed(backups):
            try:
                atomic_write(target.path, backup_path.read_text(encoding="utf-8"), info)
            except Exception:
                pass
        raise

    print(f"Applied startup handoff atomically; backups stored in {backup_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HandoffError as exc:
        print(f"Startup handoff refused: {exc}", file=sys.stderr)
        raise SystemExit(1)
