# v0.1.0 validation

Validated on Linux, Python 3.12.3, and Herdr 0.9.0.

## Acceptance suite

```text
$ python3 -m unittest discover -s tests -v
Ran 33 tests in 13.770s
OK
```

The suite runs real subprocesses in temporary Git repositories, with a fake
Herdr executable that records its arguments. It covers successful and failing
checks, ignored events, no-change controls, malformed configuration, locks,
bindings, stale agent turns, timeouts, interruption, output limits, and
fingerprints including binary, untracked, and submodule content.

## Live Herdr smoke test

```text
$ python3 tests/live_smoke.py
PASS: done event -> detached checker -> failed report -> three-line prompt delivered
PASS: unchanged failure skips without a feedback loop
PASS: repaired snapshot passes silently
PASS: unchanged pass skips; 8 live hooks succeeded, slowest 66 ms
```

This test uses a real Herdr server with separate configuration and state. Its
agent is a harmless input receiver with a recognized process name, not an AI
model. This verifies the plugin's event, runner, and prompt transport plumbing;
it does not prove a model will read the report or successfully repair the code.
The plugin linked with no manifest warnings.

The first test run caught issues in interruption cleanup and feedback targeting;
the corresponding negative controls pass in this release. A first live test
also verified that a refused Herdr prompt was recorded as a delivery failure.

## Limits of this evidence

There has been no week-long operational soak or live trial of every agent
provider. Only Linux is advertised as supported. CI additionally runs the
acceptance suite on Python 3.11, 3.12, and 3.13; consult the workflow results for
the current commit rather than inferring their status from this document.
