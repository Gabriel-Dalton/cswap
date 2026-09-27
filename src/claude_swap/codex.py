"""Codex CLI accounts alongside the Claude ones.

Each Codex account is a ``CODEX_HOME`` directory. The default one (``$CODEX_HOME``
or ``~/.codex``) is always listed when it holds a login; others are registered
in ``<backup_root>/codex/accounts.json``.

Usage comes from Codex's own session logs: every turn writes a ``token_count``
event carrying the account's ``rate_limits`` as the server reported them. That
needs no network call, but it is only as fresh as the last Codex turn on that
account, so every reading carries its age.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import paths
from claude_swap.exceptions import ClaudeSwitchError

MAIN_LIMIT_ID = "codex"
_FILES_TO_SCAN = 20
_TAIL_BYTES = 1024 * 1024
_CACHE_TTL_S = 15.0

_PLAN_LABELS = {
    "free": "Free",
    "go": "Go",
    "plus": "Plus",
    "pro": "Pro",
    "prolite": "Pro Lite",
    "team": "Team",
    "business": "Business",
    "enterprise": "Enterprise",
    "edu": "Edu",
}


@dataclass
class CodexUsage:
    """One account's latest reading, in the same window shape as Claude usage."""

    usage: dict = field(default_factory=dict)
    fetched_at: float | None = None
    plan: str | None = None
    limit_reached: bool = False

    @property
    def age_s(self) -> float | None:
        return None if self.fetched_at is None else max(0.0, time.time() - self.fetched_at)


@dataclass
class CodexAccount:
    name: str
    home: Path
    email: str | None
    plan: str | None
    is_default: bool

    @property
    def plan_label(self) -> str | None:
        if not self.plan:
            return None
        return _PLAN_LABELS.get(self.plan, self.plan.replace("_", " ").title())


# -- homes and the account store ---------------------------------------------


def default_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def _store_path() -> Path:
    return paths.get_backup_root() / "codex" / "accounts.json"


def _managed_homes_dir() -> Path:
    return paths.get_backup_root() / "codex" / "homes"


def _read_store() -> list[dict]:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    accounts = data.get("accounts") if isinstance(data, dict) else None
    return [a for a in accounts or [] if isinstance(a, dict) and a.get("name") and a.get("home")]


def _write_store(entries: list[dict]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"accounts": entries}, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _same_home(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(a.resolve()) == os.path.normcase(b.resolve())
    except OSError:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def read_identity(home: Path) -> tuple[str | None, str | None]:
    """(email, plan) from the ChatGPT login in ``home/auth.json``."""
    try:
        auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    if not isinstance(tokens, dict):
        return None, None
    claims = _jwt_claims(str(tokens.get("id_token") or ""))
    openai = claims.get("https://api.openai.com/auth")
    plan = openai.get("chatgpt_plan_type") if isinstance(openai, dict) else None
    return claims.get("email"), plan


def has_login(home: Path) -> bool:
    return (home / "auth.json").is_file()


def load_accounts() -> list[CodexAccount]:
    """The default home (when logged in) first, then every registered home."""
    default = default_home()
    registered = _read_store()
    accounts: list[CodexAccount] = []

    default_entry = next(
        (e for e in registered if _same_home(Path(e["home"]), default)), None
    )
    if has_login(default) or default_entry:
        email, plan = read_identity(default)
        name = default_entry["name"] if default_entry else "codex"
        accounts.append(CodexAccount(name, default, email, plan, True))

    for entry in registered:
        home = Path(entry["home"])
        if entry is default_entry:
            continue
        email, plan = read_identity(home)
        accounts.append(CodexAccount(entry["name"], home, email, plan, False))
    return accounts


def find_account(target: str) -> CodexAccount:
    accounts = load_accounts()
    for acc in accounts:
        if target in (acc.name, acc.email):
            return acc
    if target.isdigit() and 1 <= int(target) <= len(accounts):
        return accounts[int(target) - 1]
    names = ", ".join(a.name for a in accounts) or "none"
    raise ClaudeSwitchError(f"No Codex account '{target}' (known: {names})")


def _check_name(name: str) -> None:
    if not name or not all(c.isalnum() or c in "-_." for c in name):
        raise ClaudeSwitchError(
            f"Invalid Codex account name '{name}' (use letters, digits, '-', '_', '.')"
        )


def add_account(name: str, home: Path | None = None) -> CodexAccount:
    """Register an existing CODEX_HOME (the default one when ``home`` is None)."""
    _check_name(name)
    home = (home or default_home()).expanduser()
    if not has_login(home):
        raise ClaudeSwitchError(f"No Codex login in {home} (missing auth.json)")
    entries = _read_store()
    if any(e["name"] == name and not _same_home(Path(e["home"]), home) for e in entries):
        raise ClaudeSwitchError(f"A Codex account named '{name}' already exists")
    entries = [e for e in entries if not _same_home(Path(e["home"]), home)]
    entries.append({"name": name, "home": str(home)})
    _write_store(entries)
    return find_account(name)


def remove_account(target: str) -> CodexAccount:
    """Forget an account. Its CODEX_HOME is left on disk."""
    acc = find_account(target)
    entries = [e for e in _read_store() if not _same_home(Path(e["home"]), acc.home)]
    if acc.is_default and len(entries) == len(_read_store()):
        raise ClaudeSwitchError(
            "The default Codex login is always listed; run `codex logout` to remove it"
        )
    _write_store(entries)
    return acc


def _codex_executable() -> str:
    exe = shutil.which("codex")
    if not exe:
        raise ClaudeSwitchError("Codex CLI not found on PATH")
    return exe


def login_account(name: str) -> CodexAccount:
    """Sign a new account into its own CODEX_HOME and register it."""
    _check_name(name)
    if any(e["name"] == name for e in _read_store()):
        raise ClaudeSwitchError(f"A Codex account named '{name}' already exists")
    home = _managed_homes_dir() / name
    home.mkdir(parents=True, exist_ok=True)
    config = default_home() / "config.toml"
    if config.is_file() and not (home / "config.toml").exists():
        shutil.copy2(config, home / "config.toml")
    env = {**os.environ, "CODEX_HOME": str(home)}
    code = subprocess.call([_codex_executable(), "login"], env=env)
    if code != 0 or not has_login(home):
        raise ClaudeSwitchError(f"Codex login did not complete (exit {code})")
    return add_account(name, home)


def run_account(target: str, args: list[str]) -> None:
    """Launch Codex as ``target`` in this terminal only."""
    acc = find_account(target)
    exe = _codex_executable()
    env = {**os.environ, "CODEX_HOME": str(acc.home)}
    if sys.platform == "win32":
        sys.exit(subprocess.call([exe, *args], env=env))
    os.execve(exe, [exe, *args], env)


# -- usage from session logs ---------------------------------------------------


def _session_files(home: Path) -> list[Path]:
    files: list[tuple[float, Path]] = []
    for sub in ("sessions", "archived_sessions"):
        root = home / sub
        if not root.is_dir():
            continue
        for path in root.rglob("rollout-*.jsonl"):
            try:
                files.append((path.stat().st_mtime, path))
            except OSError:
                continue
    files.sort(reverse=True)
    return [p for _, p in files[:_FILES_TO_SCAN]]


def _tail_lines(path: Path) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            raw = fh.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > _TAIL_BYTES else lines


def _event_time(event: dict) -> float | None:
    stamp = event.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _latest_limits(home: Path) -> dict[str, tuple[float, dict]]:
    """Newest ``rate_limits`` per limit id, with the time it was written."""
    latest: dict[str, tuple[float, dict]] = {}
    for path in _session_files(home):
        for line in _tail_lines(path):
            if '"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get("payload")
            limits = payload.get("rate_limits") if isinstance(payload, dict) else None
            at = _event_time(event)
            if not isinstance(limits, dict) or at is None:
                continue
            limit_id = str(limits.get("limit_id") or MAIN_LIMIT_ID)
            if limit_id not in latest or at > latest[limit_id][0]:
                latest[limit_id] = (at, limits)
        if MAIN_LIMIT_ID in latest:
            break
    return latest


def _window(raw: object, now: float) -> tuple[int, dict] | None:
    """(window_minutes, cswap-shaped window) for one Codex window."""
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percent")
    minutes = raw.get("window_minutes")
    resets = raw.get("resets_at")
    if not isinstance(pct, (int, float)) or not isinstance(minutes, int):
        return None
    window: dict = {"pct": float(pct)}
    if isinstance(resets, (int, float)):
        if resets <= now:
            # The window rolled over since this reading; nothing is used yet.
            window["pct"] = 0.0
            resets += minutes * 60 * (1 + int((now - resets) // (minutes * 60)))
        window["resets_at"] = datetime.fromtimestamp(resets, tz=timezone.utc).isoformat()
    return minutes, window


def _slot(minutes: int) -> str | None:
    if minutes <= 6 * 60:
        return "five_hour"
    if 6 * 24 * 60 <= minutes <= 8 * 24 * 60:
        return "seven_day"
    return None


def read_usage(home: Path, now: float | None = None) -> CodexUsage:
    now = time.time() if now is None else now
    latest = _latest_limits(home)
    if not latest:
        return CodexUsage()

    usage: dict = {}
    scoped: list[dict] = []
    main_at, main = latest.get(MAIN_LIMIT_ID) or max(latest.values(), key=lambda v: v[0])
    for key in ("primary", "secondary"):
        parsed = _window(main.get(key), now)
        if parsed is None:
            continue
        slot = _slot(parsed[0])
        if slot and slot not in usage:
            usage[slot] = parsed[1]

    for limit_id, (at, limits) in sorted(latest.items()):
        if limit_id == MAIN_LIMIT_ID or now - at > 7 * 24 * 3600:
            continue
        parsed = _window(limits.get("primary"), now)
        if parsed is None:
            continue
        name = limits.get("limit_name") or limit_id.replace("_", " ")
        scoped.append({**parsed[1], "name": str(name)})
    if scoped:
        usage["scoped"] = scoped

    return CodexUsage(
        usage=usage,
        fetched_at=main_at,
        plan=main.get("plan_type"),
        limit_reached=bool(main.get("rate_limit_reached_type")),
    )


_cache: dict[str, tuple[float, CodexUsage]] = {}


def cached_usage(acc: CodexAccount) -> CodexUsage:
    """``read_usage`` with a short in-process cache for the TUI's redraws."""
    key = str(acc.home)
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    usage = read_usage(acc.home, now)
    _cache[key] = (now, usage)
    return usage


def display_plan(acc: CodexAccount, usage: CodexUsage) -> str | None:
    plan = acc.plan or usage.plan
    return _PLAN_LABELS.get(plan, plan) if plan else None


# -- JSON ----------------------------------------------------------------------


def _json_window(window: dict) -> dict:
    out = {"pct": window["pct"]}
    if "resets_at" in window:
        out["resetsAt"] = window["resets_at"]
    return out


def list_payload() -> list[dict]:
    out: list[dict] = []
    for acc in load_accounts():
        usage = read_usage(acc.home)
        item: dict = {
            "name": acc.name,
            "email": acc.email,
            "plan": display_plan(acc, usage),
            "home": str(acc.home),
            "isDefault": acc.is_default,
            "usageStatus": "ok" if usage.usage else "unavailable",
            "usage": None,
        }
        if usage.usage:
            u = usage.usage
            item["usage"] = {
                name: _json_window(u[key])
                for key, name in (("five_hour", "fiveHour"), ("seven_day", "sevenDay"))
                if key in u
            }
            if u.get("scoped"):
                item["usage"]["scoped"] = [
                    {**_json_window(w), "name": w["name"]} for w in u["scoped"]
                ]
            item["usageFetchedAt"] = datetime.fromtimestamp(
                usage.fetched_at, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            item["usageAgeSeconds"] = round(usage.age_s or 0.0, 1)
            item["limitReached"] = usage.limit_reached
        out.append(item)
    return out
