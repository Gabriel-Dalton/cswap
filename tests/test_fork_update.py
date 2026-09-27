"""Fork update notice and upgrade commands."""

from __future__ import annotations

import io
import json
import sys
from unittest.mock import patch

from claude_swap import fork_update


def _response(payload: dict):
    body = io.BytesIO(json.dumps(payload).encode())
    body.__enter__ = lambda s: s
    body.__exit__ = lambda s, *a: None
    return body


class TestSourceDir:
    def test_reads_the_directory_from_the_receipt(self, tmp_path, monkeypatch):
        src = tmp_path / "checkout"
        src.mkdir()
        (tmp_path / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "claude-swap", directory = "%s" }]\n'
            % str(src).replace("\\", "/")
        )
        monkeypatch.setattr(sys, "prefix", str(tmp_path))
        assert fork_update.source_dir() == src

    def test_none_without_a_receipt_or_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "prefix", str(tmp_path))
        assert fork_update.source_dir() is None
        (tmp_path / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "claude-swap" }]\n'
        )
        assert fork_update.source_dir() is None


class TestCheckForUpdate:
    def test_newer_upstream_release_is_reported_with_merge_hint(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fork_update, "CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr(fork_update, "source_dir", lambda: tmp_path / "src")
        with patch(
            "claude_swap.fork_update.urllib.request.urlopen",
            return_value=_response({"tag_name": "v0.28.0"}),
        ):
            msg = fork_update.check_for_update("1.0.0")
        assert msg is not None
        assert "0.28.0" in msg and fork_update.UPSTREAM_BASE in msg and "1.0.0" in msg
        assert "merge upstream/main" in msg and "cswap upgrade" in msg

    def test_same_base_is_quiet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fork_update, "CACHE_PATH", tmp_path / "cache.json")
        with patch(
            "claude_swap.fork_update.urllib.request.urlopen",
            return_value=_response({"tag_name": "v0.27.0b1"}),
        ):
            assert fork_update.check_for_update("1.0.0") is None

    def test_network_failure_is_quiet_and_cached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fork_update, "CACHE_PATH", tmp_path / "cache.json")
        with patch(
            "claude_swap.fork_update.urllib.request.urlopen", side_effect=OSError("down")
        ):
            assert fork_update.check_for_update("1.0.0") is None
        assert (tmp_path / "cache.json").exists()


class TestUpgrade:
    def test_commands_pull_and_reinstall_the_checkout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fork_update, "source_dir", lambda: tmp_path)
        monkeypatch.setattr(sys, "platform", "linux")
        assert fork_update.upgrade_commands() == [
            ["git", "-C", str(tmp_path), "pull", "--ff-only"],
            ["uv", "tool", "install", "--force", "--reinstall", str(tmp_path)],
        ]

    def test_windows_installs_into_the_tool_environment(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fork_update, "source_dir", lambda: tmp_path)
        monkeypatch.setattr(sys, "platform", "win32")
        [_pull, install] = fork_update.upgrade_commands()
        assert install[:3] == ["uv", "pip", "install"]
        assert "--reinstall-package" in install and install[-1] == str(tmp_path)

    def test_commands_fall_back_to_the_fork_git_url(self, monkeypatch):
        monkeypatch.setattr(fork_update, "source_dir", lambda: None)
        [cmd] = fork_update.upgrade_commands()
        assert cmd[-1] == f"git+https://github.com/{fork_update.FORK_REPO}"

    def test_windows_prints_instead_of_running(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(fork_update, "source_dir", lambda: tmp_path)
        monkeypatch.setattr(sys, "platform", "win32")
        with patch("claude_swap.fork_update.subprocess.run") as run:
            assert fork_update.run_self_upgrade() == 1
        run.assert_not_called()
        out = capsys.readouterr().out
        assert "pull --ff-only" in out and "--reinstall-package" in out

    def test_posix_runs_each_command_and_stops_on_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fork_update, "source_dir", lambda: tmp_path)
        monkeypatch.setattr(sys, "platform", "linux")
        with patch("claude_swap.fork_update.subprocess.run") as run:
            run.return_value.returncode = 3
            assert fork_update.run_self_upgrade() == 3
        assert run.call_count == 1
