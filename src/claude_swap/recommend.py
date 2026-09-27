"""Which account to use next, across Claude and Codex.

Weekly quota is the perishable one: whatever is unused when the window resets
is gone. Every account is scored by the share of its week still unused
divided by the days left until that reset, so the account losing the most
per day ranks first. Accounts at a limit, disabled, or without usage data are
listed with the reason they were skipped. With ``models`` given, an account
must expose a weekly window for each named model (an account without one
cannot run that model), and the binding headroom folds those windows in.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from claude_swap import oauth, poll_policy

DAY_S = 86400.0
MIN_DAYS = 0.25


@dataclass(frozen=True)
class Candidate:
    provider: str
    key: str
    label: str
    email: str | None
    weekly_pct: float | None
    weekly_reset_ts: float | None
    headroom: float | None
    limit_reached: bool = False
    disabled: bool = False
    missing_models: tuple[str, ...] = ()
    stale: bool = False

    @classmethod
    def from_usage(
        cls,
        provider: str,
        key: str,
        label: str,
        email: str | None,
        usage: dict | None,
        *,
        now: float,
        models: Sequence[str] = (),
        limit_reached: bool = False,
        disabled: bool = False,
        stale: bool = False,
    ) -> "Candidate":
        weekly = usage.get("seven_day") if isinstance(usage, dict) else None
        pct = weekly.get("pct") if isinstance(weekly, dict) else None
        reset = poll_policy.parse_reset_ts(weekly.get("resets_at")) if isinstance(weekly, dict) else None
        if reset is not None and reset <= now:
            reset = None
        scoped = usage.get("scoped") if isinstance(usage, dict) else None
        names = {
            str(w.get("name", "")).lower()
            for w in (scoped or [])
            if isinstance(w, dict)
        }
        missing = tuple(
            m for m in models if m.lower() != "all" and m.lower() not in names
        ) if models else ()
        return cls(
            provider=provider,
            key=key,
            label=label,
            email=email,
            weekly_pct=float(pct) if isinstance(pct, (int, float)) else None,
            weekly_reset_ts=reset,
            headroom=oauth.account_headroom(usage, models) if isinstance(usage, dict) else None,
            limit_reached=limit_reached,
            disabled=disabled,
            missing_models=missing,
            stale=stale,
        )

    @property
    def display(self) -> str:
        if self.email and self.email != self.label:
            return f"{self.label} ({self.email})"
        return self.label


@dataclass(frozen=True)
class Ranked:
    candidate: Candidate
    score: float | None
    skip_reason: str | None
    unused_pct: float | None
    days_left: float | None


@dataclass(frozen=True)
class Recommendation:
    ranked: tuple[Ranked, ...] = field(default_factory=tuple)
    models: tuple[str, ...] = ()

    @property
    def best(self) -> Ranked | None:
        for r in self.ranked:
            if r.skip_reason is None:
                return r
        return None


def _skip_reason(c: Candidate) -> str | None:
    if c.disabled:
        return "disabled"
    if c.missing_models:
        return "no " + ", ".join(c.missing_models)
    if c.limit_reached or (c.headroom is not None and c.headroom <= 0):
        return "at limit"
    if c.weekly_pct is None:
        return "usage unknown"
    return None


def rank(candidates: Iterable[Candidate], now: float | None = None) -> tuple[Ranked, ...]:
    now = time.time() if now is None else now
    rows: list[Ranked] = []
    for c in candidates:
        unused = None if c.weekly_pct is None else max(0.0, 100.0 - c.weekly_pct)
        days = None if c.weekly_reset_ts is None else max(0.0, (c.weekly_reset_ts - now) / DAY_S)
        reason = _skip_reason(c)
        score = None
        if reason is None and unused is not None:
            score = unused / max(days if days is not None else 7.0, MIN_DAYS)
        rows.append(Ranked(c, score, reason, unused, days))
    usable = [r for r in rows if r.skip_reason is None]
    skipped = [r for r in rows if r.skip_reason is not None]
    usable.sort(key=lambda r: (-(r.score or 0.0), -(r.candidate.headroom or 0.0)))
    return tuple(usable + skipped)


def recommend(candidates: Iterable[Candidate], *, models: Sequence[str] = (), now: float | None = None) -> Recommendation:
    return Recommendation(ranked=rank(candidates, now), models=tuple(models))


def countdown(seconds: float | None) -> str:
    if seconds is None:
        return "reset unknown"
    seconds = max(0.0, seconds)
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def describe(r: Ranked) -> str:
    if r.skip_reason is not None:
        return r.skip_reason
    left = f"{r.unused_pct:.0f}% of the week left"
    when = "resets in " + countdown(None if r.days_left is None else r.days_left * DAY_S)
    return f"{left}, {when}"


def format_lines(rec: Recommendation) -> list[str]:
    """A headline plus one line per remaining account, in rank order."""
    if not rec.ranked or all(r.skip_reason == "usage unknown" for r in rec.ranked):
        return []
    best = rec.best
    lines: list[str] = []
    suffix = f" for {', '.join(rec.models)}" if rec.models else ""
    if best is None:
        lines.append(f"Use next{suffix}: nothing usable right now")
    else:
        lines.append(f"Use next{suffix}: {best.candidate.display}, {describe(best)}")
    rest = [r for r in rec.ranked if r is not best]
    if rest:
        lines.append("then " + "; ".join(f"{r.candidate.display}: {describe(r)}" for r in rest))
    return lines


def payload(rec: Recommendation) -> dict:
    def row(r: Ranked) -> dict:
        out: dict = {
            "provider": r.candidate.provider,
            "account": r.candidate.key,
            "label": r.candidate.label,
            "email": r.candidate.email,
            "weeklyUnusedPct": None if r.unused_pct is None else round(r.unused_pct, 1),
            "weeklyResetsAt": None,
            "daysUntilReset": None if r.days_left is None else round(r.days_left, 2),
            "headroomPct": None if r.candidate.headroom is None else round(r.candidate.headroom, 1),
            "score": None if r.score is None else round(r.score, 2),
        }
        if r.candidate.weekly_reset_ts is not None:
            out["weeklyResetsAt"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(r.candidate.weekly_reset_ts)
            )
        if r.skip_reason:
            out["skipped"] = r.skip_reason
        return out

    best = rec.best
    return {
        "models": list(rec.models),
        "best": None if best is None else {"provider": best.candidate.provider, "account": best.candidate.key},
        "ranked": [row(r) for r in rec.ranked],
    }


def claude_candidates(
    accounts_info: Iterable[tuple],
    entries: dict,
    *,
    disabled: set[str] = frozenset(),
    now: float | None = None,
    models: Sequence[str] = (),
) -> list[Candidate]:
    """Candidates from the switcher's ``accounts_info`` rows and usage entries."""
    now = time.time() if now is None else now
    out: list[Candidate] = []
    for num, email, _org, _org_uuid, _active, _creds, alias in accounts_info:
        entry = entries.get(str(num))
        usage = entry.last_good if entry is not None and isinstance(entry.last_good, dict) else None
        stale = entry is not None and not isinstance(entry.decision_value(), dict)
        out.append(Candidate.from_usage(
            "claude", str(num), alias or email or str(num), email, usage,
            now=now, models=models, disabled=str(num) in disabled, stale=stale,
        ))
    return out


def snapshot_candidates(snapshot, *, now: float | None = None, models: Sequence[str] = ()) -> list[Candidate]:
    """Candidates from the TUI's :class:`AccountsSnapshot`."""
    now = time.time() if now is None else now
    out: list[Candidate] = []
    for acc in snapshot.accounts:
        usage = acc.usage.last_good if isinstance(acc.usage.last_good, dict) else None
        stale = not isinstance(acc.usage.decision_value(), dict)
        out.append(Candidate.from_usage(
            "claude", acc.number, acc.alias or acc.email or acc.number, acc.email, usage,
            now=now, models=models, disabled=acc.disabled, stale=stale,
        ))
    return out


def codex_candidates(
    now: float | None = None, models: Sequence[str] = (), *, cached: bool = False
) -> list[Candidate]:
    """Every Codex account, from its session logs. Codex runs no Claude model."""
    from claude_swap import codex

    now = time.time() if now is None else now
    out: list[Candidate] = []
    try:
        accounts = codex.load_accounts()
    except Exception:
        return out
    for acc in accounts:
        usage = codex.cached_usage(acc) if cached else codex.read_usage(acc.home, now)
        cand = Candidate.from_usage(
            "codex", acc.name, acc.name, acc.email, usage.usage,
            now=now, models=(), limit_reached=usage.limit_reached,
        )
        if models:
            cand = Candidate(**{**cand.__dict__, "missing_models": tuple(models)})
        out.append(cand)
    return out
