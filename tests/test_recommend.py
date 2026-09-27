"""Use-next ranking across providers."""

from __future__ import annotations

from datetime import datetime, timezone

from claude_swap import recommend
from claude_swap.recommend import Candidate

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc).timestamp()
DAY = 86400.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _usage(weekly_pct: float, reset_in_days: float, five_hour: float = 10.0, scoped=None) -> dict:
    usage = {
        "five_hour": {"pct": five_hour, "resets_at": _iso(NOW + 3600)},
        "seven_day": {"pct": weekly_pct, "resets_at": _iso(NOW + reset_in_days * DAY)},
    }
    if scoped:
        usage["scoped"] = scoped
    return usage


def _cand(key, weekly_pct, reset_in_days, **kw) -> Candidate:
    return Candidate.from_usage(
        "claude", key, key, f"{key}@x.org", _usage(weekly_pct, reset_in_days, **kw.pop("windows", {})),
        now=NOW, **kw,
    )


class TestRanking:
    def test_most_quota_lost_per_day_ranks_first(self):
        # a: 80% left over 2 days = 40/day; b: 90% left over 6 days = 15/day
        rec = recommend.recommend([_cand("a", 20, 2), _cand("b", 10, 6)], now=NOW)
        assert [r.candidate.key for r in rec.ranked] == ["a", "b"]
        assert rec.best.candidate.key == "a"

    def test_limit_reached_and_disabled_sort_last_with_reasons(self):
        rec = recommend.recommend([
            _cand("full", 100, 1),
            _cand("off", 0, 1, disabled=True),
            _cand("ok", 50, 3),
        ], now=NOW)
        assert [r.candidate.key for r in rec.ranked] == ["ok", "full", "off"]
        assert rec.ranked[1].skip_reason == "at limit"
        assert rec.ranked[2].skip_reason == "disabled"

    def test_five_hour_window_at_limit_counts_as_at_limit(self):
        rec = recommend.recommend([_cand("busy", 10, 3, windows={"five_hour": 100.0})], now=NOW)
        assert rec.best is None
        assert rec.ranked[0].skip_reason == "at limit"

    def test_unknown_usage_is_skipped_not_ranked(self):
        unknown = Candidate.from_usage("claude", "u", "u", None, None, now=NOW)
        rec = recommend.recommend([unknown, _cand("ok", 50, 3)], now=NOW)
        assert rec.best.candidate.key == "ok"
        assert rec.ranked[-1].skip_reason == "usage unknown"

    def test_model_filter_skips_accounts_without_that_window(self):
        fable = [{"name": "Fable", "pct": 30.0, "resets_at": _iso(NOW + 2 * DAY)}]
        rec = recommend.recommend([
            Candidate.from_usage("claude", "std", "std", None, _usage(10, 2), now=NOW, models=("fable",)),
            Candidate.from_usage("claude", "prem", "prem", None, _usage(10, 2, scoped=fable), now=NOW, models=("fable",)),
        ], models=("fable",), now=NOW)
        assert rec.best.candidate.key == "prem"
        assert rec.ranked[-1].skip_reason == "no fable"

    def test_elapsed_reset_reads_as_unknown_reset(self):
        c = _cand("old", 50, -1)
        assert c.weekly_reset_ts is None
        rec = recommend.recommend([c], now=NOW)
        assert rec.best is not None
        assert rec.ranked[0].days_left is None


class TestText:
    def test_headline_and_rest(self):
        rec = recommend.recommend([_cand("a", 20, 2), _cand("b", 100, 1)], now=NOW)
        lines = recommend.format_lines(rec)
        assert lines[0] == "Use next: a (a@x.org), 80% of the week left, resets in 2d 0h"
        assert lines[1] == "then b (b@x.org): at limit"

    def test_nothing_usable(self):
        rec = recommend.recommend([_cand("b", 100, 1)], models=("fable",), now=NOW)
        assert recommend.format_lines(rec)[0] == "Use next for fable: nothing usable right now"

    def test_silent_when_nothing_has_usage(self):
        unknown = Candidate.from_usage("claude", "u", "u", None, None, now=NOW)
        assert recommend.format_lines(recommend.recommend([unknown], now=NOW)) == []

    def test_countdown(self):
        assert recommend.countdown(2 * DAY + 3 * 3600) == "2d 3h"
        assert recommend.countdown(3 * 3600 + 120) == "3h 2m"
        assert recommend.countdown(90) == "1m"
        assert recommend.countdown(None) == "reset unknown"


class TestPayload:
    def test_shape(self):
        rec = recommend.recommend([_cand("a", 20, 2), _cand("b", 100, 1)], now=NOW)
        out = recommend.payload(rec)
        assert out["best"] == {"provider": "claude", "account": "a"}
        assert out["ranked"][0]["weeklyUnusedPct"] == 80.0
        assert out["ranked"][0]["daysUntilReset"] == 2.0
        assert out["ranked"][0]["weeklyResetsAt"] == "2026-09-29T12:00:00Z"
        assert out["ranked"][1]["skipped"] == "at limit"
        assert out["models"] == []


class TestCodex:
    def test_codex_accounts_are_candidates_and_never_run_claude_models(self, temp_home, monkeypatch):
        from claude_swap import codex

        acc = codex.CodexAccount("oasis", temp_home / ".codex", "me@x.org", "prolite", True)
        monkeypatch.setattr(codex, "load_accounts", lambda: [acc])
        monkeypatch.setattr(
            codex, "read_usage",
            lambda home, now=None: codex.CodexUsage(usage=_usage(40, 3), fetched_at=NOW - 60),
        )
        [cand] = recommend.codex_candidates(NOW)
        assert cand.provider == "codex" and cand.weekly_pct == 40.0
        [cand] = recommend.codex_candidates(NOW, models=("fable",))
        assert cand.missing_models == ("fable",)
