"""Codex accounts: identity, usage from session logs, the store, and the CLI."""

from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli, codex
from claude_swap.exceptions import ClaudeSwitchError

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc).timestamp()
DAY = 24 * 3600


@pytest.fixture(autouse=True)
def _no_codex_home_env(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def _login(home: Path, email: str = "me@example.org", plan: str = "prolite") -> None:
    home.mkdir(parents=True, exist_ok=True)
    claims = {"email": email, "https://api.openai.com/auth": {"chatgpt_plan_type": plan}}
    (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": _jwt(claims)}}))


def _event(at: float, limits: dict) -> str:
    stamp = datetime.fromtimestamp(at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return json.dumps({
        "timestamp": stamp,
        "type": "event_msg",
        "payload": {"type": "token_count", "info": None, "rate_limits": limits},
    })


def _limits(primary: dict | None, secondary: dict | None = None, **extra) -> dict:
    return {"limit_id": "codex", "primary": primary, "secondary": secondary,
            "plan_type": "prolite", **extra}


def _session(home: Path, name: str, lines: list[str]) -> Path:
    path = home / "sessions" / "2026" / "09" / "27" / f"rollout-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(['{"type":"session_meta"}', *lines]) + "\n")
    return path


class TestIdentity:
    def test_reads_email_and_plan_from_id_token(self, tmp_path):
        _login(tmp_path, "gabriel@example.org", "prolite")
        assert codex.read_identity(tmp_path) == ("gabriel@example.org", "prolite")

    def test_missing_or_broken_auth_is_unknown(self, tmp_path):
        assert codex.read_identity(tmp_path) == (None, None)
        (tmp_path / "auth.json").write_text("{not json")
        assert codex.read_identity(tmp_path) == (None, None)


class TestReadUsage:
    def test_weekly_only_window(self, tmp_path):
        week = {"used_percent": 10.0, "window_minutes": 10080, "resets_at": NOW + 6 * DAY}
        _session(tmp_path, "a", [_event(NOW - 3600, _limits(week))])

        usage = codex.read_usage(tmp_path, NOW)

        assert set(usage.usage) == {"seven_day"}
        assert usage.usage["seven_day"]["pct"] == 10.0
        assert usage.fetched_at == pytest.approx(NOW - 3600)
        assert usage.plan == "prolite"

    def test_five_hour_and_weekly_windows_by_length(self, tmp_path):
        five = {"used_percent": 42.0, "window_minutes": 300, "resets_at": NOW + 3600}
        week = {"used_percent": 60.0, "window_minutes": 10080, "resets_at": NOW + DAY}
        _session(tmp_path, "a", [_event(NOW - 60, _limits(five, week))])

        usage = codex.read_usage(tmp_path, NOW).usage

        assert usage["five_hour"]["pct"] == 42.0
        assert usage["seven_day"]["pct"] == 60.0

    def test_newest_event_wins(self, tmp_path):
        old = {"used_percent": 5.0, "window_minutes": 10080, "resets_at": NOW + DAY}
        new = {"used_percent": 30.0, "window_minutes": 10080, "resets_at": NOW + DAY}
        _session(tmp_path, "a", [_event(NOW - 600, _limits(new)), _event(NOW - 7200, _limits(old))])

        assert codex.read_usage(tmp_path, NOW).usage["seven_day"]["pct"] == 30.0

    def test_elapsed_window_reads_as_empty_with_next_reset(self, tmp_path):
        week = {"used_percent": 80.0, "window_minutes": 10080, "resets_at": NOW - DAY}
        _session(tmp_path, "a", [_event(NOW - 2 * DAY, _limits(week))])

        window = codex.read_usage(tmp_path, NOW).usage["seven_day"]

        assert window["pct"] == 0.0
        reset = datetime.fromisoformat(window["resets_at"]).timestamp()
        assert reset == pytest.approx(NOW + 6 * DAY)

    def test_other_limit_ids_become_scoped_rows(self, tmp_path):
        week = {"used_percent": 10.0, "window_minutes": 10080, "resets_at": NOW + DAY}
        premium = {"limit_id": "premium", "limit_name": None,
                   "primary": {"used_percent": 25.0, "window_minutes": 10080,
                               "resets_at": NOW + DAY}}
        stale = {"limit_id": "old_model", "limit_name": None,
                 "primary": {"used_percent": 99.0, "window_minutes": 10080,
                             "resets_at": NOW + DAY}}
        _session(tmp_path, "a", [
            _event(NOW - 60, _limits(week)),
            _event(NOW - 120, premium),
            _event(NOW - 9 * DAY, stale),
        ])

        scoped = codex.read_usage(tmp_path, NOW).usage["scoped"]

        assert [(w["name"], w["pct"]) for w in scoped] == [("premium", 25.0)]

    def test_no_logs_means_unknown(self, tmp_path):
        usage = codex.read_usage(tmp_path, NOW)
        assert usage.usage == {} and usage.fetched_at is None

    def test_limit_reached_flag(self, tmp_path):
        week = {"used_percent": 100.0, "window_minutes": 10080, "resets_at": NOW + DAY}
        _session(tmp_path, "a", [
            _event(NOW - 60, _limits(week, rate_limit_reached_type="primary"))
        ])
        assert codex.read_usage(tmp_path, NOW).limit_reached is True


class TestAccountStore:
    def test_default_home_listed_when_logged_in(self, temp_home):
        _login(temp_home / ".codex", "me@example.org")

        accounts = codex.load_accounts()

        assert [(a.name, a.email, a.is_default) for a in accounts] == [
            ("codex", "me@example.org", True)
        ]

    def test_no_default_login_lists_nothing(self, temp_home):
        assert codex.load_accounts() == []

    def test_add_names_the_default_login(self, temp_home):
        _login(temp_home / ".codex")

        codex.add_account("oasis")

        assert [a.name for a in codex.load_accounts()] == ["oasis"]

    def test_add_and_remove_another_home(self, temp_home, tmp_path):
        _login(temp_home / ".codex", "a@example.org")
        other = tmp_path / "work-home"
        _login(other, "b@example.org", "plus")

        codex.add_account("work", other)
        accounts = codex.load_accounts()
        assert [(a.name, a.email, a.plan_label) for a in accounts] == [
            ("codex", "a@example.org", "Pro Lite"),
            ("work", "b@example.org", "Plus"),
        ]
        assert codex.find_account("2").name == "work"

        codex.remove_account("work")
        assert [a.name for a in codex.load_accounts()] == ["codex"]
        assert (other / "auth.json").exists()

    def test_add_refuses_a_home_without_login(self, temp_home, tmp_path):
        with pytest.raises(ClaudeSwitchError, match="No Codex login"):
            codex.add_account("empty", tmp_path)

    def test_add_refuses_a_taken_name(self, temp_home, tmp_path):
        _login(tmp_path / "one")
        _login(tmp_path / "two")
        codex.add_account("work", tmp_path / "one")
        with pytest.raises(ClaudeSwitchError, match="already exists"):
            codex.add_account("work", tmp_path / "two")

    def test_unnamed_default_cannot_be_removed(self, temp_home):
        _login(temp_home / ".codex")
        with pytest.raises(ClaudeSwitchError, match="always listed"):
            codex.remove_account("codex")

    def test_run_sets_codex_home_for_the_child_only(self, temp_home, tmp_path):
        other = tmp_path / "work-home"
        _login(other)
        codex.add_account("work", other)

        with patch("claude_swap.codex.shutil.which", return_value="codex"), \
             patch("claude_swap.codex.subprocess.call", return_value=0) as call, \
             patch("claude_swap.codex.os.execve") as execve, \
             patch("claude_swap.codex.sys.exit"):
            codex.run_account("work", ["resume"])

        launched = call if call.called else execve
        env = launched.call_args.args[-1] if launched is execve else launched.call_args.kwargs["env"]
        assert env["CODEX_HOME"] == str(other)


class TestCli:
    def test_codex_list_json(self, temp_home, capsys):
        home = temp_home / ".codex"
        _login(home, "me@example.org")
        week = {"used_percent": 10.0, "window_minutes": 10080,
                "resets_at": datetime.now(timezone.utc).timestamp() + DAY}
        _session(home, "a", [_event(datetime.now(timezone.utc).timestamp() - 60, _limits(week))])

        with patch.object(sys, "argv", ["cswap", "codex", "list", "--json"]):
            cli.main()

        out = json.loads(capsys.readouterr().out)["codexAccounts"]
        assert out[0]["email"] == "me@example.org"
        assert out[0]["plan"] == "Pro Lite"
        assert out[0]["usage"]["sevenDay"]["pct"] == 10.0
        assert "fiveHour" not in out[0]["usage"]

    def test_codex_run_needs_a_name(self, temp_home, capsys):
        with patch.object(sys, "argv", ["cswap", "codex", "run"]), \
             pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2
        assert "needs an account name" in capsys.readouterr().err
