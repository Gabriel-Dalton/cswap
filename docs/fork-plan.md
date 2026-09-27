# Fork notes

cswap is a fork of realiti4/claude-swap. It adds Codex accounts, conversation handoff,
a use-next recommendation across providers, plan tier labels, and a few
Windows fixes. Upstream releases are merged in as they appear.

## What 1.0.0 adds over upstream 0.27.0b1

- `cswap codex`: Codex CLI accounts, each its own `CODEX_HOME`, with usage
  read from Codex's session logs (`codex.py`).
- `cswap handoff <account>` and the `/swap` slash command: copy one
  conversation into another account's profile and resume it in a new
  Windows Terminal window (`handoff.py`).
- "Use next" in `cswap list`, `list --json` (`useNext`) and the dashboard:
  every Claude and Codex account ranked by weekly quota about to expire,
  with `--model` awareness (`recommend.py`).
- Plan tier labels next to the org name (`plan_tier.py`).
- `ui.selectAction run`: selecting an account in the dashboard opens a new
  window with `cswap run` instead of rewriting the default login.
- Update check against upstream's GitHub releases and `cswap upgrade` that
  reinstalls from the checkout (`fork_update.py`).
- Symlink tests skip where symlinks cannot be created.

## Keeping upstream merges clean

Fork-owned modules: `codex.py`, `handoff.py`, `recommend.py`,
`plan_tier.py`, `fork_update.py`, `tests/test_codex.py`,
`tests/test_handoff.py`, `tests/test_recommend.py`, `tests/test_plan_tier.py`,
`tests/test_fork_update.py`, this file.

Hooks into upstream files, each a few lines: `cli.py` (pre-dispatch for
`codex` and `handoff`, `--model` on `list`, the two update-check imports),
`switcher.py` (Codex section, use-next section, plan label, `useNext` in
JSON), `json_output.py` (`plan`), `models.py` (`plan`), `settings.py`
(`ui.selectAction`), `tui/app.py` (`select_action`), `tui/dashboard.py`
(two panels), `tui/widgets.py` (Codex and use-next panels), `tui/cswap.tcss`,
`tests/conftest.py` (`require_symlinks`), `tests/test_cli.py` (update-check
patch targets).

After merging upstream: set `UPSTREAM_BASE` in `fork_update.py` to the
merged release, bump `version` in `pyproject.toml`, run `uv lock`, run the
suite, reinstall. The distribution is named `cswap`; a machine still holding the
`claude-swap` uv tool should run `uv tool uninstall claude-swap` and then
`uv tool install <checkout>` once no cswap window is open. On Windows every open `cswap run` window and dashboard
keeps the tool's launcher resident, so `uv tool install --force --reinstall`
fails to replace it; `cswap upgrade` prints the install that works there
(`uv pip install` into the tool environment).

README images are SVGs in `assets/`, regenerated with
`uv run python docs/screenshots.py` (placeholder identities; pass
`CSWAP_SCREENSHOT_SESSION=<id>` to render the handoff frame).

## Findings

- A running Claude Code session exposes `CLAUDE_CODE_SESSION_ID` and
  `CLAUDE_CONFIG_DIR` to its shell; `cswap handoff` reads both.
- Transcripts live at `<config>/projects/<slug>/<session-id>.jsonl`, where
  the slug is the cwd with every non-alphanumeric character replaced by `-`.
  A session that has not completed an exchange has no transcript yet.
- Claude Code watches `.credentials.json` by mtime and emits a
  "credentials changed on disk" event, so a running session adopts a
  rewritten credential file. That is the mechanism an in-place
  `switch --here` would rely on. Not shipped: the watcher exists for token
  rotation of the same account, whether cached identity (org, user) follows
  is unproven, cswap's profiles are keyed by account and treat identity
  drift as an invalid profile, and two live copies of one credential drift
  when the server rotates the refresh token. Handoff is the supported path.
- Sessions register under `<config>/sessions/`, so sessions on different
  config dirs cannot see or message each other. Claude only scans its own
  dir; not something cswap can bridge.
- Usage polling stays inside the measured budget: intervals of 270 to 450
  seconds per account against roughly 28 to 30 requests per hour per
  identity, zero 429s logged. Only the default login shares its identity
  with the status line and desktop widget. No change made.
- `sequence.json`'s `activeAccountNumber` is the last slot added (kept in
  step by swap and move), not the live default login.
- Stored credentials carry `subscriptionType` / `rateLimitTier`; known
  tiers map to labels in `plan_tier.py`, unknown ones show raw.
