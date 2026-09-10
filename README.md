# Kitchen Brigade

[![Acceptance tests](https://github.com/Operator-create/herdr-kitchen-brigade/actions/workflows/ci.yml/badge.svg)](https://github.com/Operator-create/herdr-kitchen-brigade/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Your agent says done. Your checks get the next word.**

Kitchen Brigade is a [Herdr](https://herdr.dev) plugin that runs your repository's
verification commands when a coding agent reaches `done`. Failed checks produce
a local report and a short message back to the same agent, so it has evidence
to work from.

In a kitchen brigade, finished plates cross the pass before service. This is
the pass for agent-written code.

```text
Agent reaches done
        |
        v
Repository opts in? -- no --> No checks
        |
        v
New snapshot? ------ no --> Record a skip
        |
        v
Run your commands --> Save report --> Pass / Fail / Error
                                         |
                                         v
                              Failure feedback to the agent
```

There is no model, API key, package install, or background service to configure.
It uses Python's standard library, Git, and the Herdr CLI.

## Install

Requires **Linux, Python 3.11+, Git, and Herdr 0.9.0+**. Your verification tools
must already be installed and available to Herdr's environment.

```sh
herdr plugin install Operator-create/herdr-kitchen-brigade --ref v0.1.0
```

Create `.kitchen-brigade.toml` at the root of a Git repository you work in:

```toml
[verify]
commands = ["npm run lint", "npm test"]
timeout_seconds = 300

[report]
on_fail = "prompt"
on_pass = "silent"
```

Use commands your project actually provides. [Examples](examples/) cover Node,
Python, and notification-only workflows. The config travels with the repository
and its worktrees. A repository without this file is skipped.

Start your agent inside that repository. When it reaches `done`, Kitchen
Brigade checks it. Herdr's command palette also provides:

| Action | Purpose |
| --- | --- |
| Kitchen Brigade: expedite | Request a check for the focused pane now |
| Kitchen Brigade: last call | Open the results board |
| Kitchen Brigade: doctor | Inspect plugin health |

No keyboard shortcut is installed automatically.

## Agents launched from a home directory

Herdr reports a pane's foreground directory, but an agent can edit files
elsewhere without changing that directory. Kitchen Brigade cannot infer which
project you meant from a conversation.

Bind that pane explicitly using the plugin's local `brigade.py` entrypoint:

```sh
python3 /path/to/herdr-kitchen-brigade/brigade.py bind \
  --pane w1:p2 --repo /path/to/project
```

`herdr plugin list --json --plugin kitchen-brigade` shows the installed
`plugin_root`. Run the command inside the same Herdr session as the agent.
Bindings are local session state, scoped to the pane's terminal identity.
Rebind when you switch projects, and remove a binding with:

```sh
python3 /path/to/herdr-kitchen-brigade/brigade.py unbind --pane w1:p2
```

## Configuration and results

`verify.commands` is an ordered list of shell command strings, run from the
repository root. `timeout_seconds` bounds the whole suite. Commands run with
standard input closed; checks must be noninteractive.

`report.on_fail` accepts `prompt`, `notify`, or `log`. `report.on_pass` accepts
`silent` or `notify`. A prompt contains at most three lines and points to the
full local report. A pane that has become blocked, working, or otherwise
changed will receive a notification fallback instead of typed feedback.

| Result | Meaning |
| --- | --- |
| Passed | Every configured command returned zero on a stable snapshot |
| Failed | A command failed or timed out; see its report |
| Error | Verification could not be completed reliably |
| Skipped | No opt-in, unchanged snapshot, or a check is already running |

The first observed snapshot is checked even if its Git working tree is clean.
A clean Git tree is not evidence that its checks pass. Later unchanged
snapshots are skipped. Fingerprints include committed, staged, unstaged, and
untracked file contents; ignored build artifacts are excluded. If the tree
changes during verification, the result cannot certify that new snapshot.

Only one verification runs at a time, across all repositories. Another working
agent in the same repository causes the check to be skipped with a notification.
Automatic duplicate feedback is suppressed to keep repeated `done` events from
creating a repair loop: one prompt per failed snapshot, and at most two prompts
per pane and repository until a check passes. Manual runs can force a fresh
check; they do not override the prompt limit.

Reports and plugin state live under `HERDR_PLUGIN_STATE_DIR`, outside the
installed source directory. The results board shows the latest outcomes;
`status --json` is available for scripts. Each report is capped at 2 MiB; the
archive retains at most 50 reports for up to 30 days (pruned on the next run).
The audit log rotates at 2 MiB and keeps one previous file.

For a foreground check, including outside Herdr:

```sh
python3 /path/to/herdr-kitchen-brigade/brigade.py run \
  --repo /path/to/project --force
```

Without `--pane`, this writes a report and sends no agent feedback. Exit codes
are `0` for success or skip, `1` for a failed check, and `2` for an operational
error. See the printed verdict to distinguish success from skip.

## What it checks

Kitchen Brigade catches what your commands catch. It cannot prove a natural
language claim, verify a citation, or discover a missing assertion. It does not
prevent an agent from sending its final answer, committing, or deploying: the
checks happen after Herdr observes `done`. It complements CI and code review.

Repository commands execute as your user with your environment, just as if you
ran them in a terminal. Use configuration from repositories you trust. Reports
may contain private source or tool output; review them before sharing. Kitchen
Brigade itself performs no network requests or telemetry, although your checks
may use the network.

Only `done` triggers an automatic run. `idle` can mean a tool pause or permission
wait, so it does not trigger checks. Agents or integrations that never emit
`done` need the manual expedite action. Deferred checks are not queued; invoke
expedite or wait for the next `done` event after contention clears.
Herdr can report a focused agent's completion as `idle`; use expedite for those
completions as well.

Checks use the current files and installed environment; this is not a hermetic
build. Ignored files and external dependencies are not part of the snapshot.
Shell commands stop on the first failure. Timeout or interruption kills the
command's process group; a command that deliberately detaches into a new
session is outside that process group. Use finite verification commands.

## Development

```sh
git clone https://github.com/Operator-create/herdr-kitchen-brigade.git
cd herdr-kitchen-brigade
python3 -m unittest discover -s tests -v
herdr plugin link "$PWD"
herdr plugin list --json --plugin kitchen-brigade
herdr plugin log list --plugin kitchen-brigade --limit 10
```

The acceptance suite uses temporary repositories and a fake Herdr executable;
it never prompts your live agents. See [CONTRIBUTING.md](CONTRIBUTING.md) for
the regression-test contract and [release notes](CHANGELOG.md) for release scope.

An optional real Herdr smoke test creates its own headless session, temporary
configuration, and a harmless agent input receiver:

```sh
python3 tests/live_smoke.py
```

It verifies real event delivery, failure feedback, a repaired pass, and skips.
It stops its server on exit and prints where it retained the evidence. It does
not invoke a model or use your normal Herdr configuration.

## Author

Created by [Cerenada_Operator / Operator-create](https://github.com/Operator-create).
Inspired by the kitchen pass: a clear handoff between making something and
checking that it is ready. AI-assisted implementation, with reproducible
acceptance tests kept in the repository.

If Kitchen Brigade helps your workflow, share a concrete example or file an
issue. Those examples will guide what comes next.

MIT licensed.
