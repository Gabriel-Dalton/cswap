"""Multi-account switcher for Claude Code."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cswap")
except PackageNotFoundError:
    __version__ = version("claude-swap")

from claude_swap.switcher import ClaudeAccountSwitcher

__all__ = ["ClaudeAccountSwitcher", "__version__"]
