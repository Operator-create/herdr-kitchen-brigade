# Community announcement draft

I built Kitchen Brigade, a small Herdr plugin inspired by the kitchen pass.

When a coding agent reaches `done`, it runs the checks your repository already
has, saves a local report, and sends failures back to that agent. Opt in with
one TOML file. It skips unchanged snapshots, enforces timeouts, and avoids
typing into a blocked or newly working agent.

Python standard library only. Linux, Python 3.11+, Herdr 0.9.0+. MIT licensed.

```sh
herdr plugin install Operator-create/herdr-kitchen-brigade --ref v0.1.0
```

Source and setup: https://github.com/Operator-create/herdr-kitchen-brigade

This is an early release with reproducible tests. I'd like feedback from people
using several coding agents: which checks do you wish ran automatically, and
where does the handoff still break down?

---

This is a draft for the author to share. It has not been posted to any community
on their behalf. Update the version and link when sharing future releases.
