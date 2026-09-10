"""Opt-in integration test using a real, isolated Herdr server.

Run: python3 tests/live_smoke.py
Creates temporary config, state, repositories and a non-model input receiver.
Never imports your Herdr configuration or addresses your existing session.
Artifacts are retained at the printed path; the test server is stopped on exit.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, label, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"Timeout: {label}")


def main():
    binary = shutil.which("herdr")
    if not binary:
        raise SystemExit("Herdr 0.9.0+ must be installed for this optional test")
    base = Path(tempfile.mkdtemp(prefix="kitchen-brigade-live-"))
    print(f"Live smoke artifacts: {base}", flush=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("HERDR_", "GIT_"))}
    env.update({
        "XDG_CONFIG_HOME": str(base / "config"),
        "XDG_STATE_HOME": str(base / "state"),
        "XDG_DATA_HOME": str(base / "data"),
        "XDG_RUNTIME_DIR": str(base / "run"),
        "HERDR_CONFIG_PATH": str(base / "config.toml"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    })
    (base / "config.toml").write_text("# Isolated Kitchen Brigade integration test\n")
    command = [binary, "--session", "kitchen-brigade-test"]

    def cli(*args):
        result = subprocess.run([*command, *args], env=env, text=True,
                                capture_output=True, timeout=15)
        if result.returncode:
            raise AssertionError(f"Herdr {args}: {result.stderr or result.stdout}")
        return json.loads(result.stdout).get("result", {}) if result.stdout.strip() else {}

    repo = base / "project with spaces"
    repo.mkdir()
    (repo / ".gitignore").write_text("runs.log\n")
    (repo / "expected.txt").write_text("broken\n")
    (repo / "checker.py").write_text(
        "from pathlib import Path\n"
        "with Path('runs.log').open('a') as output: output.write('run\\n')\n"
        "ok = Path('expected.txt').read_text().strip() == 'ready'\n"
        "print('checker: ready' if ok else 'checker: expected ready')\n"
        "raise SystemExit(0 if ok else 1)\n"
    )
    (repo / ".kitchen-brigade.toml").write_text(
        '[verify]\ncommands = ["python3 checker.py"]\ntimeout_seconds = 5\n'
        '[report]\non_fail = "prompt"\non_pass = "silent"\n'
    )
    for args in (("init", "-q"), ("add", "."),
                 ("-c", "user.name=Smoke Test", "-c", "user.email=smoke@example.invalid", "commit", "-qm", "fixture")):
        subprocess.run(["git", "-C", str(repo), *args], env=env, check=True,
                       capture_output=True, timeout=10)
    receiver = base / "receiver.py"
    # Herdr validates foreground agent process identity before sending input.
    # Give the harmless receiver the expected executable name in this test only.
    stub = base / "codex"
    stub.symlink_to(sys.executable)
    received = base / "received.txt"
    ready = base / "receiver-ready"
    receiver.write_text(
        "import sys\nfrom pathlib import Path\n"
        "Path(sys.argv[2]).touch()\n"
        "for line in sys.stdin:\n"
        "    with Path(sys.argv[1]).open('a') as output: output.write(line)\n"
    )
    log = (base / "server.log").open("wb")
    server = subprocess.Popen([*command, "server"], env=env, cwd=base,
                              stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                              start_new_session=True)
    state_root = base / "state/herdr/plugins/kitchen-brigade"
    state_file = state_root / "state.json"

    def latest():
        if not state_file.exists():
            return {}
        return json.loads(state_file.read_text())["repos"].get(str(repo), {})

    def runs():
        path = repo / "runs.log"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def events():
        path = state_root / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    try:
        socket = base / "config/herdr/sessions/kitchen-brigade-test/herdr.sock"
        wait_for(socket.exists, "isolated server socket")
        assert cli("plugin", "list", "--json")["plugins"] == [], "isolation failed"
        linked = cli("plugin", "link", str(ROOT))["plugin"]
        assert not linked.get("warnings"), linked
        cli("workspace", "create", "--cwd", str(base), "--label", "Anchor", "--no-focus")
        workspace = cli("workspace", "create", "--cwd", str(repo), "--label", "Brigade smoke", "--no-focus")
        pane = workspace["root_pane"]["pane_id"]
        cli("pane", "run", pane, shlex.join([str(stub), str(receiver), str(received), str(ready)]))
        wait_for(ready.exists, "input receiver")

        def finish(seq):
            cli("pane", "report-agent", pane, "--source", "kitchen-brigade-test", "--agent", "codex", "--state", "working", "--seq", str(seq))
            cli("pane", "report-agent", pane, "--source", "kitchen-brigade-test", "--agent", "codex", "--state", "idle", "--seq", str(seq + 1))

        finish(1)
        wait_for(lambda: latest().get("verdict") == "fail", "failing checker verdict")
        wait_for(lambda: received.exists() and "report done again." in received.read_text(), "actual failure prompt delivery")
        message = received.read_text().strip()
        assert len(message.splitlines()) == 3, message
        assert latest()["report"] in message, message
        assert "checker: expected ready" in Path(latest()["report"]).read_text()
        print("PASS: done event -> detached checker -> failed report -> three-line prompt delivered", flush=True)

        finish(3)
        wait_for(lambda: any(event.get("reason") == "unchanged_failure" for event in events()), "duplicate failure skip")
        assert runs() == 1
        assert received.read_text().strip() == message
        print("PASS: unchanged failure skips without a feedback loop", flush=True)

        (repo / "expected.txt").write_text("ready\n")
        finish(5)
        wait_for(lambda: latest().get("verdict") == "pass", "repaired snapshot passes")
        assert runs() == 2
        assert received.read_text().strip() == message
        print("PASS: repaired snapshot passes silently", flush=True)

        finish(7)
        wait_for(lambda: latest().get("reason") == "unchanged", "unchanged passing snapshot skip")
        assert runs() == 2
        logs = cli("plugin", "log", "list", "--plugin", "kitchen-brigade", "--limit", "20")["logs"]
        hooks = [item for item in logs if item.get("event") == "pane.agent_status_changed"]
        assert hooks and all(item.get("exit_code") == 0 for item in hooks), hooks
        elapsed = [item["finished_unix_ms"] - item["started_unix_ms"] for item in hooks]
        assert max(elapsed) < 1000, elapsed
        print(f"PASS: unchanged pass skips; {len(hooks)} live hooks succeeded, slowest {max(elapsed)} ms", flush=True)
        (base / "result.json").write_text(json.dumps({"result": "pass", "hooks": hooks, "state": latest()}, indent=2) + "\n")
    finally:
        if server.poll() is None:
            try:
                cli("server", "stop")
            finally:
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.terminate()
                    server.wait(timeout=5)
        log.close()


if __name__ == "__main__":
    main()
