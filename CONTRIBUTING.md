# Contributing

Bug reports, focused fixes, and examples from real workflows are welcome.

For a bug, include your operating system, Python and Herdr versions, the
command or action that triggered it, and a minimal reproduction. Redact local
paths, secrets, and private code from reports before sharing them.

Kitchen Brigade uses Python's standard library. Run the acceptance suite with:

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary Git repositories and a fake Herdr executable. They must
not send prompts to real agents, modify a developer's Herdr settings, or run
checks in their projects.

Please include a failing regression test for a behavioral fix. In particular,
changes to caching, locks, timeouts, pane targeting, and feedback should have a
negative control. Keep event hooks quick, output bounded, and failures visible.

Changes should preserve the small scope: run existing repository commands,
record their results, and route feedback. New verification languages, model
dependencies, and automatic project modifications need a separate discussion.

Contributions are licensed under the project's MIT license.
