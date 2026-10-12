"""Acceptance checks at the CLI and subprocess boundary; no live Herdr required.

Run from the plugin root with: python3 -m unittest discover -s tests -v
The fake Herdr executable records argv, so these tests cannot prompt real agents.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "brigade.py"
PANE = "test:p1"


FAKE_HERDR = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ['KB_TEST_CALLS'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps(args) + '\\n')
agents = json.loads(Path(os.environ['KB_TEST_AGENTS']).read_text())
if args[:2] == ['agent', 'list']:
    result = {'type': 'agent_list', 'agents': agents}
elif args[:2] == ['pane', 'get']:
    result = {'type': 'pane_info', 'pane': next((a for a in agents if a['pane_id'] == args[2]), {})}
elif args[:2] == ['agent', 'prompt']:
    result = {'type': 'agent_prompted', 'agent': next((a for a in agents if a['pane_id'] == args[2]), {})}
elif args[:2] == ['notification', 'show']:
    result = {'type': 'notification_show', 'shown': True, 'reason': 'shown'}
else:
    print('unexpected fake Herdr invocation: ' + repr(args), file=sys.stderr)
    sys.exit(23)
print(json.dumps({'id': 'test-cli', 'result': result}))
"""


class BrigadeAcceptance(unittest.TestCase):
    """Small real Git repositories exercise observable gate behavior."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kitchen brigade test ")
        self.base = Path(self.temp.name)
        self.repo = self.make_repo("repository with spaces")
        self.state = self.base / "state with spaces"
        self.calls_file = self.base / "herdr calls.jsonl"
        self.agents_file = self.base / "agents.json"
        self.fake = self.base / "fake herdr"
        self.fake.write_text(FAKE_HERDR)
        self.fake.chmod(0o755)
        self.env = os.environ.copy()
        for key in list(self.env):
            if key.startswith(("HERDR_", "GIT_")):
                del self.env[key]
        self.env.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "HERDR_BIN_PATH": str(self.fake),
            "HERDR_PLUGIN_ROOT": str(ROOT),
            "HERDR_PLUGIN_STATE_DIR": str(self.state),
            "HERDR_PLUGIN_CONFIG_DIR": str(self.base / "config"),
            "HERDR_SOCKET_PATH": str(self.base / "fake.sock"),
            "KB_TEST_CALLS": str(self.calls_file),
            "KB_TEST_AGENTS": str(self.agents_file),
        })
        self.children = []
        self.descendant_pids = []
        self.set_agents(self.agent())

    def tearDown(self):
        for process in self.children:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=3)
        for pid in self.descendant_pids:
            if self.process_running(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        # A detached worker outlives the assertion that observed its evidence: a
        # test can see its report while it is still writing state and snapshots.
        # Removing the tree underneath it fails with ENOTEMPTY, so retry briefly
        # rather than leaving the suite intermittently red.
        for _ in range(40):
            try:
                self.temp.cleanup()
                return
            except OSError:
                time.sleep(0.1)
        self.temp.cleanup()

    def make_repo(self, name):
        repo = self.base / name
        repo.mkdir()
        self.git(repo, "init", "-q")
        (repo / "tracked.txt").write_text("initial contents\n")
        self.git(repo, "add", "tracked.txt")
        self.git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture")
        return repo

    def git(self, repo, *args):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        result = subprocess.run(["git", "-C", str(repo), *args], text=True,
                                capture_output=True, timeout=5, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def agent(self, pane=PANE, status="done", repo=None, **extra):
        cwd = str(self.repo if repo is None else repo)
        return {"pane_id": pane, "workspace_id": "test", "agent": "codex",
                "agent_status": status, "status": status, "cwd": cwd,
                "foreground_cwd": cwd, "terminal_id": "terminal_" + pane,
                "state_change_seq": 1, "revision": 1,
                "agent_session": {"agent": "codex", "kind": "id", "source": "test",
                                  "value": "session_" + pane}, **extra}

    def set_agents(self, *agents):
        self.agents_file.write_text(json.dumps(list(agents)))

    def configure(self, commands, *, timeout=5, on_fail="prompt", on_pass="silent", repo=None):
        repo = self.repo if repo is None else repo
        (repo / ".kitchen-brigade.toml").write_text(
            "[verify]\ncommands = " + json.dumps(commands) + "\n"
            + f"timeout_seconds = {timeout}\n\n[report]\n"
            + f"on_fail = {json.dumps(on_fail)}\non_pass = {json.dumps(on_pass)}\n"
        )

    def python(self, source):
        return shlex.join([sys.executable, "-c", source])

    def counted_command(self, *, exit_code=0, sleep=0, marker="runs.txt"):
        target = self.base / marker
        return self.python(
            "from pathlib import Path; import time; "
            + f"p=Path({str(target)!r}); "
            + "p.open('a').write('run\\n'); "
            + f"time.sleep({sleep}); print('checker evidence'); raise SystemExit({exit_code})"
        )

    def run_cli(self, *args, env=None, timeout=12):
        return subprocess.run([sys.executable, str(CLI), *args], cwd=ROOT,
                              env=self.env if env is None else env, text=True,
                              capture_output=True, timeout=timeout)

    def start_cli(self, *args):
        process = subprocess.Popen([sys.executable, str(CLI), *args], cwd=ROOT,
                                   env=self.env, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        self.children.append(process)
        return process

    def run_gate(self, *, pane=PANE, force=False, repo=None):
        args = ["run", "--repo", str(self.repo if repo is None else repo)]
        if pane is not None:
            args += ["--pane", pane]
        if force:
            args += ["--force"]
        return self.run_cli(*args)

    def event(self, status="done", **data):
        env = dict(self.env)
        env["HERDR_PLUGIN_EVENT_JSON"] = json.dumps({
            "event": "pane_agent_status_changed",
            "data": {"type": "pane_agent_status_changed", "pane_id": PANE,
                     "workspace_id": "test", "agent_status": status,
                     "agent": "codex", **data},
        })
        return self.run_cli("event", env=env)

    def calls(self, *prefix):
        if not self.calls_file.exists():
            return []
        calls = [json.loads(line) for line in self.calls_file.read_text().splitlines()]
        return [call for call in calls if call[:len(prefix)] == list(prefix)]

    def reports(self):
        return sorted((self.state / "reports").glob("*.txt"))

    def runs(self, marker="runs.txt"):
        path = self.base / marker
        return path.read_text().splitlines() if path.exists() else []

    def assert_exit(self, result, code):
        self.assertEqual(result.returncode, code,
                         f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    def wait_for(self, predicate, *, timeout=5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(0.025)
        self.fail("timed out waiting for test subprocess evidence")

    @staticmethod
    def process_running(pid):
        try:
            status = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return False
        return status.rsplit(")", 1)[1].strip().split()[0] != "Z"

    def touch(self, repo):
        """Give the watched registry something new to find."""
        (repo / "tracked.txt").write_text(f"edited {repo.name} {time.monotonic()}\n")

    def test_pass_runs_configured_checker_and_silent_policy_is_silent(self):
        self.configure([self.counted_command()])
        self.assert_exit(self.run_gate(), 0)
        self.assertEqual(self.runs(), ["run"])
        self.assertEqual(self.calls("agent", "prompt"), [])
        self.assertEqual(self.calls("notification", "show"), [])
        self.assertTrue(self.reports())
        self.assertIn("checker evidence", self.reports()[-1].read_text())

    def test_failure_preserves_output_and_prompts_at_most_three_lines(self):
        self.configure([self.counted_command(exit_code=7)])
        self.assert_exit(self.run_gate(), 1)
        prompts = self.calls("agent", "prompt")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0][2], PANE)
        body = prompts[0][-1]
        self.assertLessEqual(len(body.splitlines()), 3)
        self.assertNotIn("`", body)
        self.assertIn("exit 7", body)
        summary = self.repo / ".kitchen-brigade/last-failure.txt"
        self.assertIn(str(summary), body)
        self.assertIn("checker evidence", summary.read_text())
        reports = self.reports()
        self.assertEqual(len(reports), 1)
        self.assertIn(str(reports[0]), summary.read_text())
        report = reports[0].read_text()
        self.assertIn("checker evidence", report)
        self.assertIn("7", report)
        self.assertIn("raise SystemExit(7)", report)

    def test_unchanged_tree_skips_and_force_runs(self):
        self.configure([self.counted_command()])
        self.assert_exit(self.run_gate(), 0)
        self.assert_exit(self.run_gate(), 0)
        self.assertEqual(self.runs(), ["run"])
        self.assert_exit(self.run_gate(force=True), 0)
        self.assertEqual(self.runs(), ["run", "run"])

    def test_untracked_file_content_change_invalidates_fingerprint(self):
        self.configure([self.counted_command()])
        untracked = self.repo / "untracked data.txt"
        untracked.write_text("first version\n")
        self.assert_exit(self.run_gate(), 0)
        untracked.write_text("other version\n")
        self.assert_exit(self.run_gate(), 0)
        self.assertEqual(self.runs(), ["run", "run"])

    def test_tracked_binary_content_change_invalidates_fingerprint(self):
        binary = self.repo / "binary.dat"
        binary.write_bytes(b"\0before\xff")
        self.git(self.repo, "add", "binary.dat")
        self.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "binary fixture")
        self.configure([self.counted_command()])
        binary.write_bytes(b"\0change-one\xff")
        self.assert_exit(self.run_gate(), 0)
        binary.write_bytes(b"\0change-two\xff")
        self.assert_exit(self.run_gate(), 0)
        self.assertEqual(self.runs(), ["run", "run"])

    def test_no_config_skips_quietly(self):
        self.assert_exit(self.run_gate(), 0)
        self.assertEqual(self.calls("agent", "prompt"), [])
        self.assertEqual(self.calls("notification", "show"), [])
        self.assertEqual(self.reports(), [])

    def test_malformed_config_is_loud_and_never_passes(self):
        (self.repo / ".kitchen-brigade.toml").write_text("[verify\ncommands = [")
        result = self.run_gate()
        self.assert_exit(result, 2)
        self.assertTrue(self.calls("notification", "show"))
        self.assertEqual(self.calls("agent", "prompt"), [])
        self.assertTrue(self.reports())

    def test_semantically_invalid_config_is_loud(self):
        invalid = (
            '[verify]\ncommands = []\n',
            '[verify]\ncommands = "true"\n',
            '[verify]\ncommands = ["true"]\ntimeout_seconds = 0\n',
            '[verify]\ncommands = ["true"]\n[report]\non_fail = "discard"\n',
            '[verify]\ncommands = ["true"]\n[report]\non_fail = []\n',
            '[verify]\ncommands = ["true"]\ntimeout_second = 2\n',
        )
        for config in invalid:
            with self.subTest(config=config):
                (self.repo / ".kitchen-brigade.toml").write_text(config)
                before = len(self.calls("notification", "show"))
                self.assert_exit(self.run_gate(force=True), 2)
                self.assertGreater(len(self.calls("notification", "show")), before)
                self.assertEqual(self.calls("agent", "prompt"), [])

    def test_unknown_checker_command_is_loud(self):
        self.configure(["kitchen_brigade_missing_checker_629efaf"])
        result = self.run_gate()
        self.assertIn(result.returncode, (1, 2), result.stdout + result.stderr)
        self.assertTrue(self.calls("notification", "show"))
        self.assertTrue(self.reports())

    def test_blocked_or_working_pane_is_never_prompted(self):
        self.configure([self.counted_command(exit_code=1)])
        for status in ("blocked", "working"):
            with self.subTest(status=status):
                self.set_agents(self.agent(status=status))
                result = self.run_gate(force=True)
                self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
                self.assertEqual(self.calls("agent", "prompt"), [])
        self.assertTrue(self.calls("notification", "show"))

    def test_pane_status_is_rechecked_after_checker_finishes(self):
        for status in ("blocked", "working"):
            with self.subTest(status=status):
                self.set_agents(self.agent())
                final_agents = json.dumps([self.agent(status=status)])
                source = (
                    "from pathlib import Path; "
                    + f"Path({str(self.agents_file)!r}).write_text({final_agents!r}); "
                    + "raise SystemExit(1)"
                )
                self.configure([self.python(source)])
                before = len(self.calls("notification", "show"))
                self.assert_exit(self.run_gate(force=True), 1)
                self.assertEqual(self.calls("agent", "prompt"), [])
                self.assertGreater(len(self.calls("notification", "show")), before)

    def test_replaced_pane_or_new_agent_turn_does_not_receive_stale_failure(self):
        mutations = (
            {"terminal_id": "different-terminal"},
            {"agent_session": {"agent": "codex", "kind": "id", "source": "test",
                               "value": "different-session"}},
            {"state_change_seq": 2},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.set_agents(self.agent())
                final_agents = json.dumps([self.agent(**mutation)])
                source = (
                    "from pathlib import Path; "
                    + f"Path({str(self.agents_file)!r}).write_text({final_agents!r}); "
                    + "raise SystemExit(1)"
                )
                self.configure([self.python(source)])
                self.assert_exit(self.run_gate(force=True), 1)
                self.assertEqual(self.calls("agent", "prompt"), [])

    def test_other_working_pane_in_same_repository_skips_and_notifies(self):
        self.configure([self.counted_command()])
        self.set_agents(self.agent(), self.agent(pane="test:p2", status="working"))
        self.assert_exit(self.run_gate(), 0)
        self.assertEqual(self.runs(), [])
        self.assertTrue(self.calls("notification", "show"))
        self.assertEqual(self.calls("agent", "prompt"), [])

    def test_manual_run_without_pane_is_log_only(self):
        self.configure([self.counted_command(exit_code=1)])
        self.assert_exit(self.run_gate(pane=None), 1)
        self.assertTrue(self.reports())
        self.assertEqual(self.calls("agent", "prompt"), [])
        self.assertEqual(self.calls("notification", "show"), [])

    def test_notify_and_log_failure_policies(self):
        self.configure([self.counted_command(exit_code=1)], on_fail="notify")
        self.assert_exit(self.run_gate(), 1)
        self.assertTrue(self.calls("notification", "show"))
        self.assertEqual(self.calls("agent", "prompt"), [])
        self.calls_file.write_text("")
        self.configure([self.counted_command(exit_code=1)], on_fail="log")
        self.assert_exit(self.run_gate(), 1)
        self.assertEqual(self.calls("notification", "show"), [])
        self.assertEqual(self.calls("agent", "prompt"), [])

    def test_pass_notification_is_opt_in(self):
        self.configure([self.counted_command()], on_pass="notify")
        self.assert_exit(self.run_gate(), 0)
        self.assertEqual(len(self.calls("notification", "show")), 1)
        self.assertEqual(self.calls("agent", "prompt"), [])

    def test_repository_and_global_locks_prevent_simultaneous_checks(self):
        release = self.base / "release checker"
        source = (
            "from pathlib import Path\nimport time\n"
            + f"Path({str(self.base / 'runs.txt')!r}).write_text('run\\n')\n"
            + f"while not Path({str(release)!r}).exists(): time.sleep(0.025)\n"
        )
        self.configure([self.python(source)], timeout=8)
        first = self.start_cli("run", "--repo", str(self.repo), "--pane", PANE)
        self.wait_for(lambda: self.runs() == ["run"])
        self.assertIsNone(first.poll())
        self.assert_exit(self.run_gate(force=True), 0)
        second_repo = self.make_repo("another repository with spaces")
        self.configure([self.counted_command(marker="second.txt")], repo=second_repo)
        self.assert_exit(self.run_gate(repo=second_repo, pane=None), 0)
        self.assertEqual(self.runs(), ["run"])
        self.assertEqual(self.runs("second.txt"), [])
        release.write_text("release\n")
        stdout, stderr = first.communicate(timeout=6)
        self.assertEqual(first.returncode, 0, stdout + stderr)
        self.assert_exit(self.run_gate(repo=second_repo, pane=None), 0)
        self.assertEqual(self.runs("second.txt"), ["run"])

    def test_timeout_terminates_descendants_and_releases_locks(self):
        pid_file = self.base / "descendant.pid"
        child_source = "import time; time.sleep(30)"
        source = (
            "import subprocess, sys, time; from pathlib import Path; "
            + f"child=subprocess.Popen([sys.executable, '-c', {child_source!r}]); "
            + f"Path({str(pid_file)!r}).write_text(str(child.pid)); time.sleep(30)"
        )
        self.configure([self.python(source)], timeout=1)
        started = time.monotonic()
        result = self.run_gate()
        self.assertIn(result.returncode, (1, 2), result.stdout + result.stderr)
        self.assertLess(time.monotonic() - started, 7)
        self.assertTrue(pid_file.exists())
        pid = int(pid_file.read_text())
        self.descendant_pids.append(pid)
        self.wait_for(lambda: not self.process_running(pid), timeout=2)
        self.assertIn("timeout", self.reports()[-1].read_text().lower())
        self.configure([self.counted_command()])
        self.assert_exit(self.run_gate(force=True), 0)
        self.assertEqual(self.runs(), ["run"])

    def test_terminated_runner_cleans_up_descendants_and_releases_locks(self):
        pid_file = self.base / "interrupted descendant.pid"
        source = (
            "import subprocess, sys, time; from pathlib import Path; "
            + "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            + f"Path({str(pid_file)!r}).write_text(str(child.pid)); time.sleep(30)"
        )
        self.configure([self.python(source)], timeout=30)
        runner = self.start_cli("run", "--repo", str(self.repo), "--pane", PANE)
        self.wait_for(pid_file.exists)
        pid = int(pid_file.read_text())
        self.descendant_pids.append(pid)
        runner.terminate()
        stdout, stderr = runner.communicate(timeout=6)
        self.assertNotEqual(runner.returncode, 0, stdout + stderr)
        self.wait_for(lambda: not self.process_running(pid), timeout=2)
        self.configure([self.counted_command()])
        self.assert_exit(self.run_gate(force=True), 0)
        self.assertEqual(self.runs(), ["run"])

    def test_missing_herdr_executable_is_loud_and_does_not_claim_pass(self):
        self.configure([self.counted_command()])
        env = dict(self.env, HERDR_BIN_PATH=str(self.base / "missing herdr"))
        result = self.run_cli("run", "--repo", str(self.repo), "--pane", PANE, env=env)
        self.assert_exit(result, 2)
        self.assertTrue(result.stdout.strip() or result.stderr.strip())
        self.assertEqual(self.runs(), [])

    def test_non_done_events_do_not_start_verification(self):
        self.configure([self.counted_command()])
        for status in ("idle", "working", "blocked", "unknown"):
            with self.subTest(status=status):
                self.assert_exit(self.event(status=status), 0)
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.calls("agent", "prompt"), [])

    def test_done_event_detaches_and_duplicate_failure_does_not_prompt_loop(self):
        self.configure([self.counted_command(exit_code=1, sleep=1.2)])
        started = time.monotonic()
        self.assert_exit(self.event(), 0)
        self.assertLess(time.monotonic() - started, 1.0)
        self.wait_for(lambda: len(self.calls("agent", "prompt")) == 1)
        self.assertEqual(self.runs(), ["run"])
        time.sleep(0.1)
        self.assert_exit(self.event(), 0)
        self.wait_for(lambda: "unchanged_failure" in (self.state / "events.jsonl").read_text())
        self.assertEqual(self.runs(), ["run"])
        self.assertEqual(len(self.calls("agent", "prompt")), 1)

    def test_malformed_event_does_not_crash_hook(self):
        env = dict(self.env, HERDR_PLUGIN_EVENT_JSON="{invalid json")
        self.assert_exit(self.run_cli("event", env=env), 0)
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.calls("agent", "prompt"), [])

    def test_binding_resolves_pane_launched_outside_repository(self):
        self.configure([self.counted_command()])
        self.set_agents(self.agent(repo=self.base))
        self.assert_exit(self.run_cli("bind", "--pane", PANE, "--repo", str(self.repo)), 0)
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: self.runs() == ["run"])
        self.wait_for(lambda: any("-pass.txt" in str(path) for path in self.reports()))
        self.assertEqual(self.runs(), ["run"])
        self.assert_exit(self.run_cli("unbind", "--pane", PANE), 0)
        self.assert_exit(self.run_cli("check", "--pane", PANE, "--force"), 0)
        self.assertEqual(self.runs(), ["run"])

    def test_snapshot_changed_during_check_is_not_cached_as_pass(self):
        source = (
            "from pathlib import Path; "
            + f"p=Path({str(self.base / 'runs.txt')!r}); p.open('a').write('run\\n'); "
            + "p=Path('tracked.txt'); p.write_text(p.read_text() + 'changed during check\\n')"
        )
        self.configure([self.python(source)])
        first = self.run_gate()
        self.assertIn(first.returncode, (0, 2), first.stdout + first.stderr)
        status = self.run_cli("status", "--json")
        self.assert_exit(status, 0)
        entry = json.loads(status.stdout)["repos"][str(self.repo.resolve())]
        self.assertNotEqual(entry["verdict"], "pass")
        self.assertFalse(entry.get("verified_fingerprint"))
        second = self.run_gate()
        self.assertIn(second.returncode, (0, 2), second.stdout + second.stderr)
        self.assertEqual(self.runs(), ["run", "run"])
        self.assertEqual(self.calls("agent", "prompt"), [])

    def test_output_is_bounded_and_report_permissions_are_private(self):
        self.configure([self.python("import sys; sys.stdout.write('x' * (3 * 1024 * 1024))")])
        self.assert_exit(self.run_gate(pane=None), 0)
        report = self.reports()[-1]
        self.assertLessEqual(report.stat().st_size, 2 * 1024 * 1024)
        self.assertIn("output truncated", report.read_text()[-100:])
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)

    def test_closed_stdout_does_not_disable_timeout(self):
        self.configure([self.python("import os, time; os.close(1); os.close(2); time.sleep(30)")], timeout=1)
        start = time.monotonic()
        self.assert_exit(self.run_gate(pane=None), 1)
        self.assertLess(time.monotonic() - start, 4)

    def test_timeout_kills_descendant_which_ignores_term_after_shell_exits(self):
        pid_file = self.base / "stubborn.pid"
        source = (
            "import signal, time; from pathlib import Path; import os; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            + f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
        )
        self.configure([self.python(source) + " & exit 0"], timeout=1)
        self.assert_exit(self.run_gate(pane=None), 1)
        pid = int(pid_file.read_text())
        self.descendant_pids.append(pid)
        self.wait_for(lambda: not self.process_running(pid))

    def test_binding_does_not_cross_sessions_or_replacement_terminals(self):
        self.configure([self.counted_command()])
        self.set_agents(self.agent(repo=self.base))
        self.assert_exit(self.run_cli("bind", "--pane", PANE, "--repo", str(self.repo)), 0)
        env = dict(self.env, HERDR_SOCKET_PATH=str(self.base / "different-session.sock"))
        self.assert_exit(self.run_cli("_worker", "--pane", PANE, env=env), 0)
        self.assertEqual(self.runs(), [])
        self.set_agents(self.agent(repo=self.base, terminal_id="replacement"))
        self.assert_exit(self.run_cli("_worker", "--pane", PANE), 0)
        self.assertEqual(self.runs(), [])

    def test_new_binding_survives_a_running_verification(self):
        other = self.make_repo("bound project")
        release = self.base / "release"
        self.configure([self.python(
            "import time; from pathlib import Path\n"
            + f"Path({str(self.base / 'started')!r}).touch()\n"
            + f"while not Path({str(release)!r}).exists(): time.sleep(0.025)\n"
        )])
        child = self.start_cli("run", "--repo", str(self.repo))
        self.wait_for(lambda: (self.base / "started").exists())
        self.assert_exit(self.run_cli("bind", "--pane", PANE, "--repo", str(other)), 0)
        release.touch()
        stdout, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, stdout + stderr)
        state = json.loads(self.run_cli("status", "--json").stdout)
        self.assertTrue(any(item["repo"] == str(other) for item in state["bindings"].values()))

    def test_invalid_config_without_pane_remains_log_only(self):
        (self.repo / ".kitchen-brigade.toml").write_text("[verify\n")
        self.assert_exit(self.run_gate(pane=None), 2)
        self.assertEqual(self.calls(), [])

    def test_broken_config_symlink_is_an_error(self):
        (self.repo / ".kitchen-brigade.toml").symlink_to("missing-config")
        self.assert_exit(self.run_gate(), 2)
        self.assertTrue(self.calls("notification", "show"))

    def test_submodule_working_content_invalidates_fingerprint(self):
        child = self.make_repo("submodule source")
        self.git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", str(child), "module")
        self.configure([self.counted_command()])
        self.assert_exit(self.run_gate(pane=None), 0)
        (self.repo / "module/tracked.txt").write_text("new module contents\n")
        self.assert_exit(self.run_gate(pane=None), 0)
        self.assertEqual(self.runs(), ["run", "run"])
    def test_watched_repository_resolves_a_pane_launched_outside_any_repository(self):
        self.configure([self.counted_command(exit_code=1)])
        self.set_agents(self.agent(repo=self.base))
        self.assert_exit(self.run_cli("watch", "--repo", str(self.repo)), 0)
        self.touch(self.repo)
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: len(self.calls("agent", "prompt")) == 1)
        self.assertEqual(self.runs(), ["run"])

    def test_unchanged_watched_repository_is_not_checked_again(self):
        self.configure([self.counted_command()])
        self.set_agents(self.agent(repo=self.base))
        self.assert_exit(self.run_cli("watch", "--repo", str(self.repo)), 0)
        self.touch(self.repo)
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: self.runs() == ["run"])
        self.wait_for(lambda: any("-pass.txt" in str(path) for path in self.reports()))
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: "no_watched_change" in (self.state / "events.jsonl").read_text())
        self.assertEqual(self.runs(), ["run"])

    def test_two_changed_watched_repositories_are_not_attributed_to_one_pane(self):
        second = self.make_repo("second repository")
        self.configure([self.counted_command()])
        self.configure([self.counted_command(marker="second runs.txt")], repo=second)
        self.set_agents(self.agent(repo=self.base))
        self.assert_exit(self.run_cli("watch", "--repo", str(self.repo)), 0)
        self.assert_exit(self.run_cli("watch", "--repo", str(second)), 0)
        self.touch(self.repo)
        self.touch(second)
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: bool(self.calls("notification", "show")))
        self.assertIn("ambiguous_watched", (self.state / "events.jsonl").read_text())
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.runs("second runs.txt"), [])
        self.assertEqual(self.calls("agent", "prompt"), [])

    def test_ambiguous_candidates_stay_candidates_for_the_next_event(self):
        second = self.make_repo("second repository")
        self.configure([self.counted_command()])
        self.configure([self.counted_command(marker="second runs.txt")], repo=second)
        self.set_agents(self.agent(repo=self.base))
        self.assert_exit(self.run_cli("watch", "--repo", str(self.repo)), 0)
        self.assert_exit(self.run_cli("watch", "--repo", str(second)), 0)
        self.touch(self.repo)
        self.touch(second)
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: "ambiguous_watched" in (self.state / "events.jsonl").read_text())
        self.assert_exit(self.run_cli("unwatch", "--repo", str(second)), 0)
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: self.runs() == ["run"])

    def test_inferred_repository_notifies_instead_of_typing_while_another_agent_works(self):
        self.configure([self.counted_command(exit_code=1)])
        self.set_agents(self.agent(repo=self.base), self.agent(pane="test:p2", status="working", repo=self.base))
        self.assert_exit(self.run_cli("watch", "--repo", str(self.repo)), 0)
        self.touch(self.repo)
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: any("-fail.txt" in str(path) for path in self.reports()))
        self.wait_for(lambda: bool(self.calls("notification", "show")))
        self.assertEqual(self.calls("agent", "prompt"), [])
        report = next(path for path in self.reports() if "-fail.txt" in str(path))
        self.assertIn("Prompt withheld", report.read_text())

    def test_watch_root_adopts_only_repositories_that_opted_in(self):
        second = self.make_repo("second repository")
        self.make_repo("third repository")
        self.configure([self.counted_command()])
        self.configure([self.counted_command(marker="second runs.txt")], repo=second)
        self.assert_exit(self.run_cli("watch", "--root", str(self.base)), 0)
        result = self.run_cli("watch", "--list")
        self.assert_exit(result, 0)
        self.assertIn(str(self.repo), result.stdout)
        self.assertIn(str(second), result.stdout)
        self.assertNotIn("third repository", result.stdout)

    def test_doctor_fails_when_done_events_never_produce_a_verdict(self):
        self.set_agents(self.agent(repo=self.base))
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: "no_repository_or_binding" in (self.state / "events.jsonl").read_text())
        result = self.run_cli("doctor")
        self.assert_exit(result, 2)
        self.assertIn("Done events (24h)", result.stdout)
        self.assertIn("1 dispatched, 0 verified", result.stdout)

    def test_doctor_passes_once_a_watched_repository_verifies(self):
        self.configure([self.counted_command()])
        self.set_agents(self.agent(repo=self.base))
        self.assert_exit(self.run_cli("watch", "--repo", str(self.repo)), 0)
        self.touch(self.repo)
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: any("-pass.txt" in str(path) for path in self.reports()))
        result = self.run_cli("doctor")
        self.assert_exit(result, 0)
        self.assertIn("1 dispatched, 1 verified", result.stdout)
    def test_baseline_tolerates_an_inherited_failure_and_never_prompts_for_it(self):
        self.configure([self.counted_command(exit_code=3)])
        self.assert_exit(self.run_cli("accept", "--repo", str(self.repo)), 0)
        self.assertEqual(self.runs(), ["run"])
        self.assert_exit(self.run_gate(force=True), 0)
        self.assertEqual(self.runs(), ["run", "run"])
        self.assertTrue(any("-baseline.txt" in str(path) for path in self.reports()))
        self.assertEqual(self.calls("agent", "prompt"), [])

    def test_a_different_exit_code_on_a_baselined_command_is_still_a_regression(self):
        self.configure([self.counted_command(exit_code=3)])
        self.assert_exit(self.run_cli("accept", "--repo", str(self.repo)), 0)
        self.configure([self.counted_command(exit_code=4)])
        self.assert_exit(self.run_gate(), 1)
        prompts = self.calls("agent", "prompt")
        self.assertEqual(len(prompts), 1)
        self.assertIn("exit 4", prompts[0][-1])

    def test_an_inherited_failure_does_not_hide_a_later_regression(self):
        inherited = self.counted_command(exit_code=3, marker="first.txt")
        self.configure([inherited, self.counted_command(exit_code=0, marker="second.txt")])
        self.assert_exit(self.run_cli("accept", "--repo", str(self.repo)), 0)
        self.configure([inherited, self.counted_command(exit_code=5, marker="second.txt")])
        self.assert_exit(self.run_gate(), 1)
        self.assertEqual(self.runs("first.txt"), ["run", "run"])
        self.assertEqual(self.runs("second.txt"), ["run", "run"])
        prompts = self.calls("agent", "prompt")
        self.assertEqual(len(prompts), 1)
        self.assertIn("exit 5", prompts[0][-1])

    def test_stop_on_first_failure_false_runs_the_whole_suite(self):
        first = self.counted_command(exit_code=1, marker="first.txt")
        second = self.counted_command(exit_code=0, marker="second.txt")
        (self.repo / ".kitchen-brigade.toml").write_text(
            "[verify]\ncommands = " + json.dumps([first, second]) + "\n"
            + "timeout_seconds = 5\nstop_on_first_failure = false\n\n"
            + "[report]\non_fail = \"prompt\"\non_pass = \"silent\"\n"
        )
        self.assert_exit(self.run_gate(), 1)
        self.assertEqual(self.runs("first.txt"), ["run"])
        self.assertEqual(self.runs("second.txt"), ["run"])

    def test_summary_lists_every_command_with_its_standing(self):
        inherited = self.counted_command(exit_code=3, marker="first.txt")
        self.configure([inherited, self.counted_command(exit_code=0, marker="second.txt")])
        self.assert_exit(self.run_cli("accept", "--repo", str(self.repo)), 0)
        self.configure([inherited, self.counted_command(exit_code=5, marker="second.txt")])
        self.assert_exit(self.run_gate(), 1)
        summary = (self.repo / ".kitchen-brigade/last-failure.txt").read_text()
        self.assertIn("Verdict: FAIL", summary)
        self.assertIn("pre-existing exit 3", summary)
        self.assertIn("REGRESSED exit 5", summary)
        self.assertLessEqual(len(summary.splitlines()), 60)

    def test_in_repo_summary_is_not_part_of_the_next_snapshot(self):
        self.configure([self.counted_command(exit_code=1)])
        self.assert_exit(self.run_gate(), 1)
        self.assertTrue((self.repo / ".kitchen-brigade/last-failure.txt").is_file())
        untracked = self.git(self.repo, "status", "--porcelain", "--untracked-files=all").strip()
        self.assertEqual(untracked, "?? .kitchen-brigade.toml")
        self.configure([self.counted_command()])
        self.assert_exit(self.run_gate(), 0)
        self.assert_exit(self.run_gate(), 0)
        self.assertEqual(self.runs(), ["run", "run"])

    def test_registering_a_repository_does_not_make_it_a_candidate_on_its_own(self):
        self.configure([self.counted_command()])
        self.set_agents(self.agent(repo=self.base))
        self.assert_exit(self.run_cli("watch", "--repo", str(self.repo)), 0)
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: "no_watched_change" in (self.state / "events.jsonl").read_text())
        self.assertEqual(self.runs(), [])
        self.touch(self.repo)
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: self.runs() == ["run"])

    def test_registering_several_repositories_does_not_create_an_ambiguity(self):
        second = self.make_repo("second repository")
        self.configure([self.counted_command()])
        self.configure([self.counted_command(marker="second runs.txt")], repo=second)
        self.set_agents(self.agent(repo=self.base))
        self.assert_exit(self.run_cli("watch", "--root", str(self.base)), 0)
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: "no_watched_change" in (self.state / "events.jsonl").read_text())
        self.assertNotIn("ambiguous_watched", (self.state / "events.jsonl").read_text())
        self.touch(second)
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: self.runs("second runs.txt") == ["run"])
        self.assertEqual(self.runs(), [])

    def test_unwatched_registry_still_reports_an_unresolved_pane(self):
        self.configure([self.counted_command()])
        self.set_agents(self.agent(repo=self.base))
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: "no_repository_or_binding" in (self.state / "events.jsonl").read_text())
        self.assertEqual(self.runs(), [])

    def test_accept_marks_the_snapshot_it_captured_as_seen(self):
        self.configure([self.counted_command(exit_code=2)])
        self.set_agents(self.agent(repo=self.base))
        self.assert_exit(self.run_cli("watch", "--repo", str(self.repo)), 0)
        self.touch(self.repo)
        self.assert_exit(self.run_cli("accept", "--repo", str(self.repo)), 0)
        self.assert_exit(self.run_cli("check", "--pane", PANE), 0)
        self.wait_for(lambda: "no_watched_change" in (self.state / "events.jsonl").read_text())
        self.assertEqual(self.runs(), ["run"])

    def naming_command(self, *names, exit_code=1, marker="runs.txt"):
        """A checker that prints file paths the way a linter or compiler does."""
        target = self.base / marker
        printed = "".join(f"print({name!r} + ':12:5  error  something is wrong'); " for name in names)
        return self.python(
            "from pathlib import Path; "
            + f"p=Path({str(target)!r}); p.open('a').write('run\\n'); "
            + printed
            + f"raise SystemExit({exit_code})"
        )

    def test_snapshot_digest_is_unchanged_by_keeping_per_file_digests(self):
        # Fingerprints recorded by earlier versions must stay valid, so the
        # aggregate digest may not shift when the per-file map is retained.
        (self.repo / "extra.txt").write_text("untracked\n")
        os.symlink("tracked.txt", self.repo / "link.txt")
        script = (
            "import sys; sys.path.insert(0, %r); "
            "from kitchen_brigade.runtime import capture_snapshot; "
            "import pathlib, hashlib, os, stat; "
            "snap = capture_snapshot(pathlib.Path(%r)); "
            "print(snap.digest); print(len(snap.files))"
        ) % (str(ROOT), str(self.repo))
        first = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True,
                               env=self.env, timeout=15)
        self.assertEqual(first.returncode, 0, first.stderr)
        digest, count = first.stdout.split()
        self.assertEqual(len(digest), 64)
        # tracked.txt, extra.txt and the symlink; no config file exists yet.
        self.assertEqual(int(count), 3)
        self.configure([self.counted_command()])
        again = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True,
                               env=self.env, timeout=15)
        self.assertNotEqual(again.stdout.split()[0], digest)

    def test_failure_names_which_changed_files_it_cites(self):
        self.configure([self.counted_command()])
        self.assert_exit(self.run_gate(), 0)
        (self.repo / "touched.txt").write_text("agent wrote this\n")
        (self.repo / "untouched-by-error.txt").write_text("also new\n")
        self.configure([self.naming_command("touched.txt")])
        self.assert_exit(self.run_gate(), 1)
        summary = (self.repo / ".kitchen-brigade/last-failure.txt").read_text()
        self.assertIn("This failure names 1 of them:", summary)
        self.assertIn("touched.txt", summary)
        self.assertNotIn("untouched-by-error.txt", summary)

    def test_failure_that_cites_nothing_you_changed_says_so(self):
        self.configure([self.counted_command()])
        self.assert_exit(self.run_gate(), 0)
        (self.repo / "touched.txt").write_text("agent wrote this\n")
        self.configure([self.naming_command("vendor/legacy.js")])
        self.assert_exit(self.run_gate(), 1)
        summary = (self.repo / ".kitchen-brigade/last-failure.txt").read_text()
        self.assertIn("names none of them", summary)

    def test_first_check_admits_it_cannot_attribute(self):
        self.configure([self.naming_command("tracked.txt")])
        self.assert_exit(self.run_gate(), 1)
        summary = (self.repo / ".kitchen-brigade/last-failure.txt").read_text()
        self.assertIn("no earlier snapshot", summary)

    def test_an_agent_editing_the_checks_is_reported(self):
        self.configure([self.counted_command()])
        self.assert_exit(self.run_gate(), 0)
        self.configure([self.counted_command(exit_code=1)])
        self.assert_exit(self.run_gate(), 1)
        summary = (self.repo / ".kitchen-brigade/last-failure.txt").read_text()
        self.assertIn(f"ALERT: {'.kitchen-brigade.toml'} changed", summary)
        alarms = [call for call in self.calls("notification", "show") if "checks were edited" in call[2]]
        self.assertEqual(len(alarms), 1)
        self.assertIn("config_edited", (self.state / "events.jsonl").read_text())
        doctor = self.run_cli("doctor")
        self.assert_exit(doctor, 2)
        self.assertIn("Checks edited by an agent", doctor.stdout)

    def test_editing_the_checks_outside_an_agent_turn_is_not_an_alarm(self):
        self.configure([self.counted_command()])
        self.assert_exit(self.run_gate(pane=None), 0)
        self.configure([self.counted_command(exit_code=1)])
        self.assert_exit(self.run_gate(pane=None), 1)
        self.assertNotIn("config_edited", (self.state / "events.jsonl").read_text())

    def test_accept_refuses_to_record_a_baseline_from_an_incomplete_suite(self):
        self.configure([self.counted_command(sleep=3)], timeout=1)
        self.assert_exit(self.run_cli("accept", "--repo", str(self.repo)), 2)
        self.configure([self.counted_command(exit_code=1)])
        self.assert_exit(self.run_gate(force=True), 1)
        self.assertEqual(len(self.calls("agent", "prompt")), 1)


if __name__ == "__main__":
    unittest.main()
