"""Move one conversation to another account.

``cswap switch`` moves the default login for every window at once. A handoff
copies a single transcript into the target account's profile and opens a new
terminal resuming it there, so the window it came from can simply be closed.
Transcripts live at ``<config>/projects/<cwd-slug>/<session-id>.jsonl``; a
sibling ``<session-id>/`` directory, when present, travels with the file.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from claude_swap import paths
from claude_swap.exceptions import ClaudeSwitchError

SESSION_ID_ENV = "CLAUDE_CODE_SESSION_ID"
SLASH_COMMAND_NAME = "swap"

SLASH_COMMAND_TEXT = """---
description: Move this conversation to another cswap account in a new window
allowed-tools: Bash(cswap handoff:*)
---
Run exactly this with the Bash tool and nothing else:

    cswap handoff $ARGUMENTS

It copies this conversation to that account and opens a new terminal window
resuming it there. Relay the command's output, then tell the user to close
this window and continue in the new one. Do not run any other command.
"""


@dataclass
class Handoff:
    session_id: str
    source: Path
    target_dir: Path
    target_transcript: Path
    account_num: str
    email: str
    default_login: bool
    command: list[str]


def project_slug(cwd: str | Path) -> str:
    """Claude's folder name for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def source_config_dir() -> Path:
    return paths.get_claude_config_home()


def find_transcript(
    config_dir: Path, session_id: str | None = None, cwd: str | Path | None = None
) -> Path:
    projects = config_dir / "projects"
    if session_id:
        hits = sorted(projects.glob(f"*/{session_id}.jsonl")) if projects.is_dir() else []
        if not hits:
            raise ClaudeSwitchError(
                f"No transcript for session {session_id} under {projects}. Claude "
                "writes it after the first exchange; send one message and retry."
            )
        return hits[0]
    folder = projects / project_slug(cwd or os.getcwd())
    candidates = [p for p in folder.glob("*.jsonl") if p.is_file()] if folder.is_dir() else []
    if not candidates:
        raise ClaudeSwitchError(f"No conversations for {cwd or os.getcwd()} under {projects}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def copy_transcript(src: Path, target_dir: Path, *, force: bool = False) -> Path:
    dest_folder = target_dir / "projects" / src.parent.name
    dest = dest_folder / src.name
    sibling = src.parent / src.stem
    dest_sibling = dest_folder / src.stem
    if not force and (dest.exists() or dest_sibling.exists()):
        raise ClaudeSwitchError(
            f"{target_dir} already holds session {src.stem}; pass --force to overwrite it"
        )
    dest_folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    if sibling.is_dir():
        if dest_sibling.exists():
            shutil.rmtree(dest_sibling)
        shutil.copytree(sibling, dest_sibling)
    return dest


def resume_command(account: str, session_id: str, default_login: bool) -> list[str]:
    if default_login:
        return ["claude", "--resume", session_id]
    return ["cswap", "run", account, "--", "--resume", session_id]


def terminal_launcher() -> str | None:
    if sys.platform != "win32":
        return None
    return shutil.which("wt") or shutil.which("wt.exe")


def open_new_terminal(command: list[str], cwd: str | Path) -> bool:
    """Start ``command`` under cmd in a fresh window; False when unavailable."""
    if sys.platform != "win32":
        return False
    wt = terminal_launcher()
    if wt:
        subprocess.Popen(
            [wt, "-w", "new", "-d", str(cwd), "cmd", "/k", *command], close_fds=True
        )
    else:
        subprocess.Popen(
            ["cmd", "/k", *command],
            cwd=str(cwd),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            close_fds=True,
        )
    return True


def _same_path(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(a.resolve()) == os.path.normcase(b.resolve())
    except OSError:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


def prepare(
    switcher,
    account: str,
    *,
    session_id: str | None,
    cwd: str | Path,
    force: bool = False,
) -> Handoff:
    """Resolve the target, copy the transcript, and return what to launch."""
    from claude_swap.session import SessionManager

    account_num, email, org_uuid = switcher.resolve_account(account)
    source_dir = source_config_dir()
    session_id = session_id or os.environ.get(SESSION_ID_ENV) or None
    src = find_transcript(source_dir, session_id, cwd)
    session_id = src.stem

    current = switcher._get_current_account()
    default_login = current is not None and current == (email, org_uuid)
    if default_login:
        target_dir = paths.get_default_claude_config_home()
    else:
        manager = SessionManager(switcher)
        target_dir, account_num, email = manager.setup_session(account_num, share=True)

    if _same_path(source_dir, target_dir):
        raise ClaudeSwitchError(
            f"Session {session_id} is already on Account-{account_num} ({email})"
        )
    dest = copy_transcript(src, target_dir, force=force)
    return Handoff(
        session_id=session_id,
        source=src,
        target_dir=target_dir,
        target_transcript=dest,
        account_num=account_num,
        email=email,
        default_login=default_login,
        command=resume_command(account_num, session_id, default_login),
    )


def install_slash_command(claude_home: Path | None = None) -> Path:
    home = claude_home or paths.get_default_claude_config_home()
    path = home / "commands" / f"{SLASH_COMMAND_NAME}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SLASH_COMMAND_TEXT, encoding="utf-8")
    return path


def command(argv: list[str]) -> None:
    """Handle ``cswap handoff <account> [--session ID] [--cwd PATH] [--no-launch] [--force]``."""
    from claude_swap.printer import accent, dimmed, error, muted
    from claude_swap.switcher import ClaudeAccountSwitcher

    parser = argparse.ArgumentParser(
        prog="cswap handoff",
        description=(
            "Copy one conversation to another account and resume it there in a "
            "new terminal window. Nothing else changes: the default login, other "
            "windows and the status line stay where they are."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap handoff 2                  move this window's conversation to account 2
  cswap handoff team625 --session 0f3c...   a specific session id
  cswap handoff 2 --no-launch      copy only, print the command to run
  cswap handoff --install-command  install the /swap slash command
        """,
    )
    parser.add_argument("account", nargs="?", metavar="NUM|EMAIL|ALIAS")
    parser.add_argument(
        "--session",
        metavar="ID",
        help=f"Session id to move (default: ${SESSION_ID_ENV}, else the newest for --cwd)",
    )
    parser.add_argument(
        "--cwd",
        metavar="PATH",
        default=os.getcwd(),
        help="Working directory of the conversation (default: here)",
    )
    parser.add_argument(
        "--no-launch", action="store_true", help="Copy the transcript but do not open a window"
    )
    parser.add_argument(
        "--force", action="store_true", help="Overwrite a copy the target account already holds"
    )
    parser.add_argument(
        "--install-command",
        action="store_true",
        help=f"Write ~/.claude/commands/{SLASH_COMMAND_NAME}.md and exit",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        if args.install_command:
            path = install_slash_command()
            print(f"Installed {accent('/' + SLASH_COMMAND_NAME)} at {path}")
            print(dimmed("Session profiles pick it up on their next `cswap run`."))
            return
        if not args.account:
            parser.error("an account is required (or --install-command)")

        switcher = ClaudeAccountSwitcher(debug=args.debug)
        result = prepare(
            switcher, args.account, session_id=args.session, cwd=args.cwd, force=args.force
        )
        where = "the default login" if result.default_login else "its session profile"
        print(
            f"Copied session {muted(result.session_id)} to "
            f"Account-{result.account_num} ({accent(result.email)}), {where}."
        )
        shown = " ".join(result.command)
        if args.no_launch:
            print(f"Resume it with: {accent(shown)}")
            return
        if open_new_terminal(result.command, args.cwd):
            print(f"Opened a new window running {accent(shown)}")
            print(dimmed("Close this window once the new one is up."))
        else:
            print(f"Open a new terminal in {args.cwd} and run: {accent(shown)}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)
