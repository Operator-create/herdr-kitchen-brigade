# Changelog

## Unreleased

- Register repositories with `watch`, so a pane started outside any repository
  no longer resolves to nothing. Version 0.1.0 skipped every such completion
  silently, which on a machine whose agents all start from a home directory was
  every completion it ever saw.
- Check the watched repository that changed since the plugin last looked. Type
  feedback back to the finishing pane only when exactly one changed and no other
  agent is working, and notify instead of guessing when attribution is ambiguous.
- `doctor` now reports outcomes as well as wiring: completions dispatched,
  verdicts attributed to a pane, skip reasons, crashes, and the watched registry.
  Completions that never verify fail the check. Foreground runs do not count.
- `status --json` includes the watched registry and the same daily activity.
- Record what a repository already fails with `accept`, and report only
  regressions against it. A repository with pre-existing failures could not use
  version 0.1.0 at all: every agent was blamed for what it inherited.
- A pre-existing failure no longer stops the suite, so a stale lint error can no
  longer hide the typecheck behind it. New verdict `baseline` means nothing
  regressed.
- `verify.stop_on_first_failure` makes running the whole suite a choice.
- Name the failing command, its exit code and its last meaningful output line in
  the three lines sent to the agent, instead of only a file path.
- Keep the per-file digests the snapshot already computed, and report which of
  the files changed since the last check are named in the failure. The aggregate
  digest is unchanged, so fingerprints recorded by earlier versions stay valid.
- Alert when `.kitchen-brigade.toml` changed in a snapshot verified for an agent
  pane: in the report, in the summary, in `doctor`, and by notification unless
  `report.on_fail` is `log`.
- Write a short failure summary to `.kitchen-brigade/last-failure.txt` inside the
  repository and point the agent at that, because agents whose file tools are
  bounded to the project cannot read the plugin state directory. The directory
  ignores itself and never enters a snapshot.

## 0.1.0

First community release.

- Run repository-owned checks after a Herdr agent reaches `done`.
- Opt in with `.kitchen-brigade.toml`; explicitly bind home-launched panes.
- Save local verification reports and send concise failure feedback.
- Skip unchanged snapshots, serialize checks, and enforce timeouts.
- Inspect results through a popup board, JSON status, and health checks.
- Include examples and reproducible acceptance tests.

Initial support is Linux with Python 3.11+ and Herdr 0.9.0+. This release has no
remote runner, baseline ratchet, or week-long operational soak claim.
