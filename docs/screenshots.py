"""Regenerate the README images with placeholder accounts.

    uv run python docs/screenshots.py

Writes SVGs into ``assets/``: the dashboard, the watch and switch screens,
and terminal renders of ``cswap list`` and ``cswap handoff``. TUI frames use
the test suite's fake switcher with made-up accounts. The CLI frames run the
real commands and replace this machine's identities with placeholders; the
identities come from cswap's own account store at run time, so nothing
personal is written anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

from rich.console import Console  # noqa: E402
from rich.text import Text  # noqa: E402

from claude_swap import codex  # noqa: E402
from claude_swap.usage_store import UsageEntry  # noqa: E402
from test_tui import FakeSwitcher, make_account, make_app, settle  # noqa: E402

ASSETS = ROOT / "assets"
SIZE = (108, 34)
PLACEHOLDER_EMAILS = {"1": "you@example.org", "2": "you@gmail.example", "3": "you@nonprofit.example"}
PLACEHOLDER_ORG = "Nonprofit, Inc."


def _iso_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _entry(pct5: float, pct7: float, *, scoped: list[tuple[str, float]] | None = None) -> UsageEntry:
    last_good: dict = {
        "five_hour": {"pct": pct5, "resets_at": _iso_in(3 * 3600 + 900)},
        "seven_day": {"pct": pct7, "resets_at": _iso_in(3 * 86400 + 4 * 3600)},
    }
    if scoped:
        last_good["scoped"] = [
            {"name": name, "pct": pct, "resets_at": _iso_in(3 * 86400 + 4 * 3600)}
            for name, pct in scoped
        ]
    now = time.time()
    return UsageEntry(last_good=last_good, fetched_at=now - 40, age_s=40.0)


def _accounts():
    return [
        dataclasses.replace(
            make_account(1, active=True, alias="max20", email=PLACEHOLDER_EMAILS["1"],
                         entry=_entry(45.0, 45.0, scoped=[("Fable", 12.0)])),
            org_name=f"{PLACEHOLDER_EMAILS['1']}'s Organization", plan="Max 20x",
        ),
        dataclasses.replace(
            make_account(2, alias="team625", email=PLACEHOLDER_EMAILS["2"],
                         entry=_entry(3.0, 3.0, scoped=[("Fable", 0.0)])),
            org_name=PLACEHOLDER_ORG, plan="Team premium seat",
        ),
        dataclasses.replace(
            make_account(3, alias="team125", email=PLACEHOLDER_EMAILS["3"],
                         entry=_entry(5.0, 1.0)),
            org_name=PLACEHOLDER_ORG, plan="Team standard seat",
        ),
    ]


def _fake_codex():
    acc = codex.CodexAccount("oasis", Path.home() / ".codex", PLACEHOLDER_EMAILS["3"], "prolite", True)
    usage = codex.CodexUsage(
        usage={"seven_day": {"pct": 10.0, "resets_at": _iso_in(6 * 86400 + 13 * 3600)}},
        fetched_at=time.time() - 15 * 60,
        plan="prolite",
    )
    codex.load_accounts = lambda: [acc]
    codex.cached_usage = lambda a: usage
    codex.read_usage = lambda home, now=None: usage


async def _tui_frames() -> None:
    _fake_codex()
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(FakeSwitcher(_accounts(), Path(tmp)))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.pause(0.3)
            app.save_screenshot("dashboard.svg", path=str(ASSETS))
            await pilot.press("w")
            await settle(pilot)
            await pilot.pause(0.3)
            app.save_screenshot("watch.svg", path=str(ASSETS))
            await pilot.press("escape")
            await settle(pilot)
            await pilot.press("s")
            await settle(pilot)
            await pilot.pause(0.3)
            app.save_screenshot("switch.svg", path=str(ASSETS))


def _placeholders(switcher) -> list[tuple[re.Pattern, str]]:
    """Replacements for this machine's identities, built from the account store."""
    rows: list[tuple[re.Pattern, str]] = []
    data = switcher._get_sequence_data() or {}
    for num, acc in (data.get("accounts") or {}).items():
        email = acc.get("email") or ""
        if email:
            rows.append((re.compile(re.escape(email)), PLACEHOLDER_EMAILS.get(str(num), f"you{num}@example.org")))
        org = acc.get("organizationName") or ""
        if org and email not in org:
            rows.append((re.compile(re.escape(org)), PLACEHOLDER_ORG))
    for acc in codex.load_accounts():
        if acc.email:
            rows.append((re.compile(re.escape(acc.email)), PLACEHOLDER_EMAILS["3"]))
    rows.append((re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "0f3c1e2a-7c1d-4b7e-9a5f-2d8e6c4b1a90"))
    rows.append((re.compile(r"[A-Za-z]:\\Users\\[^\\ ]+"), r"C:\\Users\\you"))
    return rows


def _sanitize(text: str, rows: list[tuple[re.Pattern, str]]) -> str:
    for pattern, repl in rows:
        text = pattern.sub(repl, text)
    return "\n".join(ln for ln in text.splitlines() if "Not sharing" not in ln)


def _capture(fn) -> str:
    from claude_swap.printer import force_color

    buf = io.StringIO()
    with force_color(), contextlib.redirect_stdout(buf):
        with contextlib.suppress(SystemExit):
            fn()
    return buf.getvalue()


def _terminal_svg(name: str, title: str, ansi: str, width: int = 100) -> None:
    console = Console(record=True, width=width, file=io.StringIO(), force_terminal=True)
    console.print(Text.from_ansi(ansi.rstrip()))
    console.save_svg(str(ASSETS / name), title=title)


def _cli_frames() -> None:
    from claude_swap.switcher import ClaudeAccountSwitcher

    switcher = ClaudeAccountSwitcher()
    rows = _placeholders(switcher)
    _terminal_svg("list.svg", "cswap list", _sanitize(_capture(switcher.list_accounts), rows))

    from claude_swap import handoff

    sid = os.environ.get("CSWAP_SCREENSHOT_SESSION")
    if sid:
        out = _capture(lambda: handoff.command(["3", "--session", sid, "--no-launch", "--force"]))
        _terminal_svg("handoff.svg", "cswap handoff", _sanitize(out, rows))


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    _cli_frames()
    asyncio.run(_tui_frames())
    for p in sorted(ASSETS.glob("*.svg")):
        print(f"{p.name}: {p.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
