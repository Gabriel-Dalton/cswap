"""Handoff: moving one conversation to another account."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli, handoff
from claude_swap.exceptions import ClaudeSwitchError

SID = "0f3c1e2a-1111-4222-8333-444455556666"


def _transcript(config: Path, slug: str, sid: str, mtime: float | None = None) -> Path:
    folder = config / "projects" / slug
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_text('{"type":"user","sessionId":"%s"}\n' % sid)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class TestSlug:
    def test_every_non_alphanumeric_becomes_a_hyphen(self):
        assert handoff.project_slug(r"C:\Users\me\repo.x") == "C--Users-me-repo-x"
        assert handoff.project_slug("/home/me/repo") == "-home-me-repo"


class TestFindTranscript:
    def test_by_session_id_in_any_project(self, tmp_path):
        want = _transcript(tmp_path, "C--elsewhere", SID)
        _transcript(tmp_path, "C--here", "other")
        assert handoff.find_transcript(tmp_path, SID) == want

    def test_missing_session_id_says_when_claude_writes_it(self, tmp_path):
        with pytest.raises(ClaudeSwitchError, match="after the first exchange"):
            handoff.find_transcript(tmp_path, SID)

    def test_newest_for_cwd_when_no_id(self, tmp_path):
        cwd = "C:/work/repo"
        slug = handoff.project_slug(cwd)
        _transcript(tmp_path, slug, "old", mtime=1_000)
        new = _transcript(tmp_path, slug, "new", mtime=2_000)
        assert handoff.find_transcript(tmp_path, None, cwd) == new

    def test_no_conversations_for_cwd(self, tmp_path):
        with pytest.raises(ClaudeSwitchError, match="No conversations"):
            handoff.find_transcript(tmp_path, None, tmp_path / "empty")


class TestCopyTranscript:
    def test_copies_file_and_sibling_folder_under_same_slug(self, tmp_path):
        src = _transcript(tmp_path / "src", "C--repo", SID)
        (src.parent / SID).mkdir()
        (src.parent / SID / "tool-result.txt").write_text("x")

        dest = handoff.copy_transcript(src, tmp_path / "dst")

        assert dest == tmp_path / "dst" / "projects" / "C--repo" / f"{SID}.jsonl"
        assert dest.read_text() == src.read_text()
        assert (dest.parent / SID / "tool-result.txt").read_text() == "x"
        assert src.exists(), "the source is copied, never moved"

    def test_refuses_to_overwrite_without_force(self, tmp_path):
        src = _transcript(tmp_path / "src", "C--repo", SID)
        _transcript(tmp_path / "dst", "C--repo", SID)
        with pytest.raises(ClaudeSwitchError, match="--force"):
            handoff.copy_transcript(src, tmp_path / "dst")
        handoff.copy_transcript(src, tmp_path / "dst", force=True)


class TestResumeCommand:
    def test_profile_target_goes_through_cswap_run(self):
        assert handoff.resume_command("2", SID, False) == [
            "cswap", "run", "2", "--", "--resume", SID,
        ]

    def test_default_login_target_runs_plain_claude(self):
        assert handoff.resume_command("1", SID, True) == ["claude", "--resume", SID]


class TestOpenNewTerminal:
    def test_false_off_windows(self, monkeypatch):
        monkeypatch.setattr(handoff.sys, "platform", "linux")
        assert handoff.open_new_terminal(["claude"], ".") is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Terminal only")
    def test_uses_wt_new_window_running_cmd_in_cwd(self, monkeypatch):
        monkeypatch.setattr(handoff.shutil, "which", lambda name: r"C:\wt.exe")
        calls = []
        monkeypatch.setattr(handoff.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
        assert handoff.open_new_terminal(["cswap", "run", "2"], r"C:\repo") is True
        assert calls == [
            [r"C:\wt.exe", "-w", "new", "-d", r"C:\repo", "cmd", "/k", "cswap", "run", "2"]
        ]

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows console only")
    def test_falls_back_to_cmd_console_without_wt(self, monkeypatch):
        monkeypatch.setattr(handoff.shutil, "which", lambda name: None)
        calls = []
        monkeypatch.setattr(
            handoff.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw["cwd"]))
        )
        assert handoff.open_new_terminal(["cswap", "run", "2"], r"C:\repo") is True
        assert calls == [(["cmd", "/k", "cswap", "run", "2"], r"C:\repo")]


class _Switcher:
    def __init__(self, current, backup_dir=None):
        self.current = current
        self.backup_dir = backup_dir or Path("unused")
        self.lock_file = self.backup_dir / "lock"
        self.platform = None

    def resolve_account(self, ident):
        return {"1": ("1", "one@x.org", "org1"), "2": ("2", "two@x.org", "org2")}[ident]

    def _get_current_account(self):
        return self.current


class TestPrepare:
    def test_moves_from_default_login_into_a_profile(self, tmp_path, monkeypatch):
        home = tmp_path / ".claude"
        _transcript(home, "C--repo", SID)
        profile = tmp_path / "profile-2"
        monkeypatch.setattr(handoff, "source_config_dir", lambda: home)
        monkeypatch.delenv(handoff.SESSION_ID_ENV, raising=False)
        with patch("claude_swap.session.SessionManager") as manager:
            manager.return_value.setup_session.return_value = (profile, "2", "two@x.org")
            result = handoff.prepare(
                _Switcher(("one@x.org", "org1")), "2", session_id=SID, cwd=tmp_path
            )
        manager.return_value.setup_session.assert_called_once_with("2", share=True)
        assert result.default_login is False
        assert result.target_transcript == profile / "projects" / "C--repo" / f"{SID}.jsonl"
        assert result.command == ["cswap", "run", "2", "--", "--resume", SID]

    def test_session_id_defaults_to_the_environment(self, tmp_path, monkeypatch):
        home = tmp_path / ".claude"
        _transcript(home, "C--repo", SID)
        monkeypatch.setattr(handoff, "source_config_dir", lambda: home)
        monkeypatch.setenv(handoff.SESSION_ID_ENV, SID)
        with patch("claude_swap.session.SessionManager") as manager:
            manager.return_value.setup_session.return_value = (
                tmp_path / "profile-2", "2", "two@x.org",
            )
            result = handoff.prepare(
                _Switcher(("one@x.org", "org1")), "2", session_id=None, cwd=tmp_path
            )
        assert result.session_id == SID

    def test_moves_from_a_profile_to_the_default_login(self, tmp_path, monkeypatch):
        profile = tmp_path / "profile-2"
        _transcript(profile, "C--repo", SID)
        home = tmp_path / ".claude"
        monkeypatch.setattr(handoff, "source_config_dir", lambda: profile)
        monkeypatch.setattr(handoff.paths, "get_default_claude_config_home", lambda: home)
        result = handoff.prepare(
            _Switcher(("one@x.org", "org1")), "1", session_id=SID, cwd=tmp_path
        )
        assert result.default_login is True
        assert result.command == ["claude", "--resume", SID]
        assert (home / "projects" / "C--repo" / f"{SID}.jsonl").exists()

    def test_refuses_when_already_on_that_account(self, tmp_path, monkeypatch):
        home = tmp_path / ".claude"
        _transcript(home, "C--repo", SID)
        monkeypatch.setattr(handoff, "source_config_dir", lambda: home)
        monkeypatch.setattr(handoff.paths, "get_default_claude_config_home", lambda: home)
        with pytest.raises(ClaudeSwitchError, match="already on Account-1"):
            handoff.prepare(_Switcher(("one@x.org", "org1")), "1", session_id=SID, cwd=tmp_path)


class TestSlashCommand:
    def test_install_writes_the_command_file(self, tmp_path):
        path = handoff.install_slash_command(tmp_path)
        assert path == tmp_path / "commands" / "swap.md"
        text = path.read_text()
        assert "cswap handoff $ARGUMENTS" in text
        assert "allowed-tools: Bash(cswap handoff:*)" in text


class TestCli:
    def test_handoff_needs_an_account(self, capsys):
        with patch.object(sys, "argv", ["cswap", "handoff"]), pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
        assert "account is required" in capsys.readouterr().err

    def test_install_command_flag(self, temp_home, capsys):
        with patch.object(sys, "argv", ["cswap", "handoff", "--install-command"]):
            cli.main()
        assert (temp_home / ".claude" / "commands" / "swap.md").exists()
        assert "/swap" in capsys.readouterr().out
