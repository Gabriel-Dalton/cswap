"""Update checks for this fork.

The upstream package on PyPI does not carry the fork's additions, so the
notice compares the fork's base version against upstream's latest GitHub
release and ``cswap upgrade`` reinstalls from the checkout the tool was
installed from (or from the fork's git URL when there is no checkout).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

from claude_swap.cache import CACHE_DIR, MISSING, read_cache, write_cache
from claude_swap.update_check import _is_newer

UPSTREAM_REPO = "realiti4/claude-swap"
UPSTREAM_BASE = "0.27.0b1"
FORK_REPO = "Gabriel-Dalton/cswap"
DIST_NAME = "cswap"
UPSTREAM_LATEST_URL = f"https://api.github.com/repos/{UPSTREAM_REPO}/releases/latest"
CACHE_PATH = CACHE_DIR / "fork_update_check.json"
CACHE_TTL = 24 * 3600


def source_dir() -> Path | None:
    """The checkout ``uv tool install`` was run from, if the receipt names one."""
    receipt = Path(sys.prefix) / "uv-receipt.toml"
    try:
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for req in data.get("tool", {}).get("requirements", []):
        directory = req.get("directory") if isinstance(req, dict) else None
        if directory and Path(directory).is_dir():
            return Path(directory)
    return None


def fetch_upstream_version() -> str | None:
    req = urllib.request.Request(
        UPSTREAM_LATEST_URL, headers={"Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(req, timeout=2) as resp:
        data = json.loads(resp.read().decode())
    tag = str(data.get("tag_name") or "")
    return tag.lstrip("v") or None


def check_for_update(current_version: str) -> str | None:
    """A one-line notice when upstream has released past ``UPSTREAM_BASE``.

    ``current_version`` is the fork's own version and only decorates the
    message; bump ``UPSTREAM_BASE`` on every upstream merge.
    """
    try:
        cached = read_cache(CACHE_PATH, CACHE_TTL)
        if cached is not MISSING:
            latest = cached
        else:
            try:
                latest = fetch_upstream_version()
            except Exception:
                latest = None
            write_cache(CACHE_PATH, latest)
        base = UPSTREAM_BASE
        if not latest or not _is_newer(latest, base):
            return None
        src = source_dir()
        where = f"git -C {src} " if src else "git "
        return (
            f"Upstream claude-swap {latest} is out; this build ({current_version}) "
            f"is based on {base}. "
            f"Merge it with `{where}fetch upstream && {where}merge upstream/main`, "
            f"then `cswap upgrade` to reinstall."
        )
    except Exception:
        return None


def upgrade_commands() -> list[list[str]]:
    src = source_dir()
    target = str(src) if src else f"git+https://github.com/{FORK_REPO}"
    if sys.platform == "win32":
        install = [
            "uv", "pip", "install", "--python", sys.executable,
            "--reinstall-package", DIST_NAME, target,
        ]
    else:
        install = ["uv", "tool", "install", "--force", "--reinstall", target]
    if src is None:
        return [install]
    return [["git", "-C", str(src), "pull", "--ff-only"], install]


def run_self_upgrade() -> int:
    """Reinstall this fork.

    Windows only prints the commands: every open ``cswap run`` window and
    dashboard keeps the tool's launcher resident, so ``uv tool install``
    cannot replace it; installing into the tool environment can.
    """
    from claude_swap.printer import accent, error

    commands = upgrade_commands()
    if sys.platform == "win32":
        print("To upgrade this fork on Windows, run:")
        for cmd in commands:
            print(f"  {accent(subprocess.list2cmdline(cmd))}")
        return 1
    for cmd in commands:
        try:
            rc = subprocess.run(cmd, check=False).returncode
        except FileNotFoundError:
            error(f"`{cmd[0]}` is not on PATH; run the upgrade from a shell where it is.")
            return 1
        if rc != 0:
            return rc
    return 0
