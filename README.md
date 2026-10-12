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
stop_on_first_failure = true

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

Herdr reports a pane's foreground directory. Most agents are started from a home
directory and never change it, so that directory names no repository at all. A
pane like that resolves to nothing and its checks are skipped.

Register the repositories you work in once, and Kitchen Brigade will use them
when a pane resolves to nothing:

```sh
python3 /path/to/herdr-kitchen-brigade/brigade.py watch --root ~/projects
```

`--root` adopts every repository directly beneath that directory that already
carries `.kitchen-brigade.toml`; it never enrols a project that has not opted in.
Use `--repo` for a single repository, `--list` to see the registry, and
`unwatch --repo` to remove one. `herdr plugin list --json --plugin
kitchen-brigade` shows the installed `plugin_root`.

When a pane resolves to nothing, the plugin fingerprints each watched repository
and checks the one that changed since it last looked. Failure feedback is typed
back to the finishing pane only when exactly one watched repository changed and
no other agent is working; otherwise you get a notification, because inference
cannot prove which agent did the work. If two watched repositories changed at
once, nothing runs and the notification names them. Those repositories stay
candidates, so the next completion says so again rather than forgetting.

A pane that does sit inside its repository needs none of this. You can also bind
one explicitly, which takes precedence over the registry:

```sh
python3 /path/to/herdr-kitchen-brigade/brigade.py bind \
  --pane w1:p2 --repo /path/to/project
```

Run it inside the same Herdr session as the agent. Bindings are local session
state, scoped to the pane's terminal identity, and are removed with
`unbind --pane w1:p2`.

## Configuration and results

`verify.commands` is an ordered list of shell command strings, run from the
repository root. `timeout_seconds` bounds the whole suite. Commands run with
standard input closed; checks must be noninteractive.

`report.on_fail` accepts `prompt`, `notify`, or `log`. `report.on_pass` accepts
`silent` or `notify`. A prompt contains at most three lines: the command that
failed with its exit code, that command's last meaningful output line, and where
to read the rest. A pane that has become blocked, working, or otherwise changed
will receive a notification fallback instead of typed feedback.

`verify.stop_on_first_failure` defaults to true. Set it to false to run the whole
suite and report every regression at once, rather than sending the agent back for
each one in turn.

## Repositories that are not already green

Most repositories fail something before an agent touches them. Left alone, the
gate blames every agent for failures it inherited, twice, and then goes quiet.
Record what the repository already fails:

```sh
python3 /path/to/herdr-kitchen-brigade/brigade.py accept --repo /path/to/project
```

`accept` runs the whole suite regardless of exit codes and stores each failing
command's exit code as that repository's baseline. Afterwards a command that
fails with its baselined exit code is reported as pre-existing and never stops
the suite, so a stale lint error can no longer hide the typecheck behind it. Any
other exit code, including a newly failing command, is a regression and is fed
back to the agent. Run `accept` again whenever you deliberately change what the
repository tolerates.

A baseline is keyed by the exact command string, so editing a command in
`.kitchen-brigade.toml` discards its licence rather than carrying it over.

## Which failures are yours

The snapshot records a digest per file, so the plugin knows exactly which paths
changed since it last looked. When a check fails it reads that command's output,
picks out the paths the checker named, and reports the overlap:

```text
Changed since the last check: 2 path(s).
This failure names 1 of them:
  src/beta.js
```

A bare filename counts only when exactly one changed path has that name, because
guessing between two would be worse than saying nothing. When a failure names
none of the files that changed, the summary says so, which usually means the
failure predates the work rather than being caused by it.

If `.kitchen-brigade.toml` is one of the files that changed in a snapshot
verified for an agent pane, the report and summary carry an alert, `doctor`
fails, and a notification is sent unless `report.on_fail` is `log`. An agent
editing its own checks is the one event this plugin most needs to surface. You
will also see it when you edit the configuration yourself and an agent completes
before the next check; a foreground run does not raise it.

## Where the agent reads the failure

On a failure the plugin writes a short summary, at most sixty lines, to
`.kitchen-brigade/last-failure.txt` inside the repository, and points the agent
there. The full output stays in the plugin state directory, which many agents
cannot read because their file tools are bounded to the project. The directory
ignores itself, so it never becomes part of a snapshot.

| Result | Meaning |
| --- | --- |
| Passed | Every configured command returned zero on a stable snapshot |
| Baseline | Nothing regressed; the only failures match the recorded baseline |
| Failed | A command failed or timed out; see its report |
| Error | Verification could not be completed reliably |
| Skipped | No opt-in, unchanged snapshot, an ambiguous attribution, or a check is already running |

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

`doctor` reports outcomes, not only wiring. It reads the plugin's own audit log
and prints how many completions were dispatched in the last day, how many
produced a verdict for a pane, and why the rest were skipped. Completions that
never verify are a failure, and it exits non-zero. A foreground run carries no
pane, so it does not count toward that total. Check it whenever the gate has
been quiet.

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
