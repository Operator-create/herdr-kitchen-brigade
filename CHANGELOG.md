# Changelog

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
