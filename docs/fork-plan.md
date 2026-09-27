# Fork plan

Review of the fork as of 2026-09-27 and the order of work. The brief that
prompted it lives outside the tree.

## Baseline

`uv run pytest -q -p no:cacheprovider`: 4 failed, 2107 passed, 77 skipped.
All four failures create symlinks, which needs a privilege on Windows.

## Findings

- A running Claude Code session exposes `CLAUDE_CODE_SESSION_ID` and
  `CLAUDE_CONFIG_DIR` to its shell, so a slash command can hand both to
  `cswap handoff` without guessing.
- Transcript folders are `<config>/projects/<slug>/`, where the slug is the
  cwd with every non-alphanumeric character replaced by `-`. A session may
  have no transcript on disk yet (only `session-env/<id>`), so handoff must
  search by id across project folders, fall back to the newest transcript
  for the cwd, and report clearly when nothing is found.
- The uv tool receipt points at this directory, so `uv tool upgrade
  claude-swap` reinstalls the fork, not PyPI. The update check never fires:
  `_parse_version("0.26.0+codex.1")` raises and the error is swallowed.
- Stored credentials carry `subscriptionType` / `rateLimitTier`:
  `max` / `default_claude_max_20x`, `team` / `default_claude_max_5x`,
  `team` / `default_raven`. Labels need a small mapping with the raw tier
  as fallback.
- Usage polling is within budget: zero 429s in the log, intervals of
  270 / 450 / 300 s against a cap of roughly 28 to 30 requests per hour per
  identity. Only the default login shares its identity with the status
  line and the desktop widget.
- `sequence.json` records `activeAccountNumber: 3` while the live
  credentials belong to account 1. Check what the field means before using it.
- The per-profile `sessions/<pid>.<hash>.key` registry is why sessions on
  different config dirs cannot see each other. Claude scans only its own
  config dir, so this is not cheap to fix from cswap.

### Codex module

- `limit_reached` survives a window rollover: usage is zeroed but the flag
  stays true, so the card warns and any ranking would skip a fresh account.
- `CodexPanel.render` sets `self.display` inside `render`; move it to the
  refresh timer.
- `add_account` accepts a digits-only name that `find_account` resolves
  positionally. Reject it.
- `CodexAccount.plan_label` and `display_plan` duplicate each other with
  different fallbacks. Keep one.
- Ctrl+C while Codex runs on Windows prints "Operation cancelled" from the
  parent while the child may still be running.
- Scoped limits found only in older session files are dropped once the
  main limit is found. The `credits` block in the log is unused.

## Order of work

1. Skip the four symlink tests when symlinks cannot be created.
2. Handoff, levels 1 and 2: `handoff.py` with a pre-dispatch hook in
   `cli.py`. Locate the transcript, bootstrap the target profile without
   launching, copy transcript and sibling folder, refuse to overwrite,
   launch `wt.exe -w new -d <cwd> cswap run <acct> -- --resume <id>`,
   print the close-window line. Handing off to the default login launches
   plain `claude --resume`. Ship `/swap` as a user command that calls the
   handoff with the two environment variables. Verify end to end. README
   section describing `cswap switch` as the all-or-nothing option.
3. Update check against upstream GitHub releases with the same cache;
   `cswap upgrade` prints the pull-and-reinstall command for this directory.
4. Codex fixes listed above, with tests.
5. Plan tier labels in list, JSON and dashboard.
6. Use-next recommendation in `recommend.py`, reusing `account_headroom`,
   the weekly reset helper and `pace`: rank by unused weekly percent over
   time to reset, skip limit-reached, honour `--model` through scoped
   windows, include Codex. Surface in list, JSON and dashboard.
7. Dashboard setting so selecting an account launches `cswap run <n>` in a
   new terminal instead of rewriting the default login.
8. Level 3 experiment (per-window profile copy and `switch --here`), plus a
   README note on the session registry gap.
9. Poll pressure: report only.
