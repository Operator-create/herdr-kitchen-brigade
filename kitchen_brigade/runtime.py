"""Runtime for the Kitchen Brigade Herdr plugin.

The event hook intentionally does almost nothing: it validates a done event and
starts a detached worker.  Repository inspection and verification happen only in
the worker or the foreground ``run`` command.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import termios
import time
import tomllib
import traceback
import tty
from dataclasses import dataclass
from typing import Any, BinaryIO, Iterator, Mapping, Sequence


PLUGIN_ID = "kitchen-brigade"
CONFIG_NAME = ".kitchen-brigade.toml"
STATE_VERSION = 1
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_REPORTS = 50
MAX_REPORT_AGE_DAYS = 30
MAX_FEEDBACKS_PER_CYCLE = 2
HERDR_CALL_TIMEOUT = 8.0
MAX_AUDIT_BYTES = 2 * 1024 * 1024


class BrigadeError(Exception):
    """Expected operational error."""


class ConfigError(BrigadeError):
    """An opt-in config exists but is invalid."""


class BusyError(BrigadeError):
    """A verification lock is already held."""


@dataclass(frozen=True)
class Config:
    commands: tuple[str, ...]
    timeout_seconds: float
    on_fail: str
    on_pass: str


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int | None
    timed_out: bool
    duration_seconds: float


@dataclass
class ReportWriter:
    stream: BinaryIO
    remaining: int = MAX_REPORT_BYTES
    truncated: bool = False

    def write(self, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        if self.remaining <= 0:
            self.truncated = True
            return
        chunk = data[: self.remaining]
        self.stream.write(chunk)
        self.stream.flush()
        self.remaining -= len(chunk)
        if len(chunk) != len(data):
            self.truncated = True

    def finish(self) -> None:
        if self.truncated:
            marker = b"\n[output truncated at Kitchen Brigade's 2 MiB report limit]\n"
            # Keep the report strictly bounded, including the marker.
            self.stream.seek(0, os.SEEK_END)
            size = self.stream.tell()
            if size + len(marker) > MAX_REPORT_BYTES:
                self.stream.truncate(max(0, MAX_REPORT_BYTES - len(marker)))
                self.stream.seek(0, os.SEEK_END)
            self.stream.write(marker)
            self.stream.flush()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def state_dir() -> pathlib.Path:
    value = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if value:
        return pathlib.Path(value)
    base = pathlib.Path(os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state"))
    return base / "herdr/plugins" / PLUGIN_ID


def plugin_root() -> pathlib.Path:
    value = os.environ.get("HERDR_PLUGIN_ROOT")
    if value:
        return pathlib.Path(value)
    return pathlib.Path(__file__).resolve().parent.parent


def ensure_state_dirs() -> pathlib.Path:
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "reports").mkdir(exist_ok=True, mode=0o700)
    (root / "locks").mkdir(exist_ok=True, mode=0o700)
    return root


def append_event(kind: str, **fields: Any) -> None:
    """Append a compact audit record; event logging itself must never crash a hook."""
    try:
        root = ensure_state_dirs()
        rec = {"time": utc_now(), "kind": kind, **fields}
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
        with (root / "locks/audit.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = root / "events.jsonl"
            if path.exists() and path.stat().st_size >= MAX_AUDIT_BYTES:
                os.replace(path, root / "events.previous.jsonl")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8", "replace"))
            finally:
                os.close(fd)
    except Exception as exc:
        print(f"Kitchen Brigade could not write its audit log: {exc}", file=sys.stderr)


def atomic_write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, indent=2, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def load_state() -> dict[str, Any]:
    path = ensure_state_dirs() / "state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("state root is not an object")
        if value.get("version") != STATE_VERSION:
            raise ValueError("unsupported state version")
        for field in ("repos", "bindings", "feedback"):
            entries = value.get(field)
            if not isinstance(entries, dict) or any(not isinstance(item, dict) for item in entries.values()):
                raise ValueError(f"invalid {field} state")
        return value
    except FileNotFoundError:
        return {"version": STATE_VERSION, "repos": {}, "bindings": {}, "feedback": {}}
    except Exception as exc:
        raise BrigadeError(f"cannot read state {path}; original preserved: {exc}") from exc


def save_state(value: Mapping[str, Any]) -> None:
    atomic_write_json(ensure_state_dirs() / "state.json", value)


@contextlib.contextmanager
def state_transaction() -> Iterator[dict[str, Any]]:
    """Serialize short read/modify/write operations, including pane bindings."""
    with (ensure_state_dirs() / "locks/state.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state()
        yield state
        save_state(state)


def session_scope() -> str | None:
    socket = os.environ.get("HERDR_SOCKET_PATH")
    if not socket:
        return None
    # The absolute socket name scopes durable bindings without exposing it in state keys.
    return hashlib.sha256(os.fsencode(os.path.abspath(socket))).hexdigest()[:24]


def binding_key(scope: str, pane_id: str) -> str:
    return f"{scope}:{pane_id}"


def herdr_bin() -> str:
    binary = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not binary:
        raise BrigadeError("Herdr not found; set HERDR_BIN_PATH or add herdr to PATH")
    return binary


def herdr_json(*args: str, timeout: float = HERDR_CALL_TIMEOUT) -> Any:
    try:
        proc = subprocess.run(
            [herdr_bin(), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BrigadeError(f"herdr {' '.join(args)} failed: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:500]
        raise BrigadeError(f"herdr {' '.join(args)} exited {proc.returncode}: {detail}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise BrigadeError(f"herdr {' '.join(args)} returned invalid JSON") from exc


def result_payload(value: Any) -> Any:
    if isinstance(value, dict) and "result" in value:
        return value["result"]
    return value


def list_agents() -> list[dict[str, Any]]:
    value = result_payload(herdr_json("agent", "list"))
    if isinstance(value, dict):
        value = value.get("agents")
    if not isinstance(value, list):
        raise BrigadeError("herdr agent list JSON has no agents array")
    return [item for item in value if isinstance(item, dict)]


def pane_info(pane_id: str) -> dict[str, Any]:
    value = result_payload(herdr_json("pane", "get", pane_id))
    if isinstance(value, dict) and isinstance(value.get("pane"), dict):
        value = value["pane"]
    if not isinstance(value, dict):
        raise BrigadeError(f"pane {pane_id} was not found")
    return value


def pane_identity(pane: Mapping[str, Any]) -> dict[str, Any]:
    """Identity fields which distinguish a reused pane id from the original pane."""
    identity: dict[str, Any] = {"terminal_id": pane.get("terminal_id"), "agent": pane.get("agent")}
    session = pane.get("agent_session")
    if isinstance(session, dict):
        identity["agent_session"] = {
            key: session.get(key) for key in ("agent", "kind", "source", "value")
        }
    else:
        identity["agent_session"] = None
    return identity


def same_identity(expected: Mapping[str, Any], pane: Mapping[str, Any]) -> bool:
    current = pane_identity(pane)
    # terminal_id is mandatory for a safe binding; fail closed on older/odd data.
    return bool(expected.get("terminal_id")) and dict(expected) == current


def context_pane_id() -> str | None:
    if os.environ.get("HERDR_PANE_ID"):
        return os.environ["HERDR_PANE_ID"]
    raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "")
    if raw:
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                for key in ("focused_pane_id", "pane_id"):
                    if isinstance(value.get(key), str) and value[key]:
                        return value[key]
        except json.JSONDecodeError:
            pass
    return None


def git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(repo), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BrigadeError(f"git {' '.join(args)} failed: {exc}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[:500]
        raise BrigadeError(f"git {' '.join(args)} exited {result.returncode}: {detail}")
    return result


def repo_root(path: str | os.PathLike[str]) -> pathlib.Path:
    start = pathlib.Path(path).expanduser()
    result = git(start, "rev-parse", "--show-toplevel")
    raw = result.stdout.rstrip(b"\n")
    if not raw:
        raise BrigadeError(f"not inside a git repository: {start}")
    return pathlib.Path(os.fsdecode(raw)).resolve()


def valid_binding(pane: Mapping[str, Any], state: Mapping[str, Any]) -> pathlib.Path | None:
    scope = session_scope()
    pane_id = pane.get("pane_id")
    if not scope or not isinstance(pane_id, str):
        return None
    bindings = state.get("bindings", {})
    item = bindings.get(binding_key(scope, pane_id)) if isinstance(bindings, dict) else None
    if not isinstance(item, dict) or not same_identity(item.get("identity", {}), pane):
        return None
    path = item.get("repo")
    if not isinstance(path, str):
        return None
    try:
        return repo_root(path)
    except BrigadeError:
        return None


def resolve_pane_repo(pane: Mapping[str, Any], state: Mapping[str, Any]) -> pathlib.Path | None:
    bound = valid_binding(pane, state)
    if bound is not None:
        return bound
    for key in ("foreground_cwd", "cwd"):
        value = pane.get(key)
        if isinstance(value, str) and value:
            try:
                return repo_root(value)
            except BrigadeError:
                continue
    return None


def load_config(repo: pathlib.Path) -> Config | None:
    path = repo / CONFIG_NAME
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ConfigError(f"{CONFIG_NAME} must be a regular file")
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ConfigError(f"{CONFIG_NAME} exceeds 64 KiB")
    except FileNotFoundError:
        if path.is_symlink():
            raise ConfigError(f"{CONFIG_NAME} is a broken symlink")
        return None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid {CONFIG_NAME}: {exc}") from exc
    verify = value.get("verify")
    if not isinstance(verify, dict):
        raise ConfigError("missing [verify] table")
    if set(value) - {"verify", "report"} or set(verify) - {"commands", "timeout_seconds"}:
        raise ConfigError("unknown configuration key; expected [verify] commands/timeout_seconds and [report]")
    commands = verify.get("commands")
    if not isinstance(commands, list) or not commands or any(
        not isinstance(item, str) or not item.strip() for item in commands
    ):
        raise ConfigError("verify.commands must be a non-empty array of command strings")
    timeout = verify.get("timeout_seconds", 300)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 86400:
        raise ConfigError("verify.timeout_seconds must be between 1 and 86400")
    report = value.get("report", {})
    if not isinstance(report, dict):
        raise ConfigError("[report] must be a table")
    if set(report) - {"on_fail", "on_pass"}:
        raise ConfigError("unknown [report] key; expected on_fail/on_pass")
    on_fail = report.get("on_fail", "prompt")
    on_pass = report.get("on_pass", "silent")
    if not isinstance(on_fail, str) or on_fail not in {"prompt", "notify", "log"}:
        raise ConfigError("report.on_fail must be prompt, notify, or log")
    if not isinstance(on_pass, str) or on_pass not in {"silent", "notify"}:
        raise ConfigError("report.on_pass must be silent or notify")
    return Config(tuple(commands), float(timeout), on_fail, on_pass)


def _hash_file(hasher: Any, root: pathlib.Path, relative: bytes) -> None:
    full = os.path.join(os.fsencode(root), relative)
    try:
        metadata = os.lstat(full)
    except FileNotFoundError:
        hasher.update(b"missing\0")
        return
    hasher.update(str(metadata.st_mode).encode("ascii") + b"\0")
    if os.path.islink(full):
        hasher.update(b"link\0" + os.fsencode(os.readlink(full)) + b"\0")
        return
    if stat.S_ISDIR(metadata.st_mode):
        # Git represents submodules and nested repositories by a directory entry.
        nested = pathlib.Path(os.fsdecode(full))
        if repo_root(nested) != nested.resolve():
            raise BrigadeError(f"cannot fingerprint tracked directory: {nested}")
        hasher.update(b"repository\0" + fingerprint(nested).encode("ascii"))
        return
    if not stat.S_ISREG(metadata.st_mode):
        hasher.update(b"special\0")
        return
    contents = hashlib.sha256()
    with open(full, "rb", buffering=0) as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            contents.update(chunk)
    hasher.update(contents.digest())


def fingerprint(repo: pathlib.Path) -> str:
    """Hash HEAD, index entries, and every tracked/untracked working-tree file.

    Git output is kept as bytes and NUL-delimited, so unusual and non-UTF-8 paths
    are unambiguous.  The config is added even if a repository ignores dotfiles.
    """
    head = git(repo, "rev-parse", "--verify", "HEAD", check=False)
    head_value = head.stdout.rstrip(b"\n") if head.returncode == 0 else b"<unborn>"
    index = git(repo, "ls-files", "-s", "-z").stdout
    names = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
    paths = {item for item in names.split(b"\0") if item}
    config_bytes = os.fsencode(CONFIG_NAME)
    if os.path.lexists(os.path.join(os.fsencode(repo), config_bytes)):
        paths.add(config_bytes)
    hasher = hashlib.sha256()
    hasher.update(b"kitchen-brigade-fingerprint-v3\0HEAD\0" + head_value + b"\0INDEX\0" + index)
    for relative in sorted(paths):
        hasher.update(b"PATH\0" + relative + b"\0")
        _hash_file(hasher, repo, relative)
        hasher.update(b"\0")
    return hasher.hexdigest()


@contextlib.contextmanager
def verification_locks(repo: pathlib.Path) -> Iterator[None]:
    root = ensure_state_dirs() / "locks"
    repo_key = hashlib.sha256(os.fsencode(os.fspath(repo))).hexdigest()
    streams = []
    try:
        for path in (root / "global.lock", root / f"repo-{repo_key}.lock"):
            stream = path.open("a+", encoding="ascii")
            streams.append(stream)
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise BusyError("another Kitchen Brigade verification is running") from exc
                raise
            stream.seek(0)
            stream.truncate()
            stream.write(f"pid={os.getpid()} started={utc_now()} repo={repo}\n")
            stream.flush()
        yield
    finally:
        for stream in reversed(streams):
            with contextlib.suppress(OSError):
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()


def report_path(repo: pathlib.Path, verdict: str) -> pathlib.Path:
    digest = hashlib.sha256(os.fsencode(os.fspath(repo))).hexdigest()[:10]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in repo.name)[:32] or "repo"
    return ensure_state_dirs() / "reports" / f"{safe}-{digest}-{stamp}-{os.getpid()}-{verdict}.txt"


def prune_reports() -> None:
    directory = ensure_state_dirs() / "reports"
    try:
        items = sorted(
            (item for item in directory.iterdir() if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        cutoff = time.time() - MAX_REPORT_AGE_DAYS * 86400
        for index, item in enumerate(items):
            if index >= MAX_REPORTS or item.stat().st_mtime < cutoff:
                with contextlib.suppress(OSError):
                    item.unlink()
    except OSError as exc:
        append_event("retention_error", error=str(exc))


def run_command(command: str, repo: pathlib.Path, deadline: float, writer: ReportWriter) -> CommandResult:
    started = time.monotonic()
    remaining = deadline - started
    if remaining <= 0:
        return CommandResult(command, None, True, 0.0)
    try:
        process = subprocess.Popen(
            ["/bin/sh", "-c", command],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            bufsize=0,
        )
    except OSError as exc:
        writer.write(f"Unable to start command: {exc}\n")
        return CommandResult(command, 127, False, time.monotonic() - started)
    assert process.stdout is not None
    os.set_blocking(process.stdout.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    timed_out = False
    try:
        # A check may close stdout while it keeps running, or exit while a child
        # holds the pipe open. Both cases remain subject to the suite deadline.
        while process.poll() is None or selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(timeout=min(0.25, remaining))
            for key, _ in events:
                try:
                    data = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if data:
                    writer.write(data)
                else:
                    selector.unregister(key.fileobj)
    finally:
        # Kill the group even if the shell already exited. A descendant may have
        # ignored TERM or redirected output. Never leave a checker daemon behind.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=0.25)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)
        selector.close()
        process.stdout.close()
    return CommandResult(
        command,
        None if timed_out else process.returncode,
        timed_out,
        time.monotonic() - started,
    )


def notify(title: str, body: str) -> tuple[bool, str | None]:
    try:
        value = result_payload(herdr_json("notification", "show", title, "--body", body, "--sound", "request"))
        if not isinstance(value, dict) or value.get("shown") is not True:
            reason = value.get("reason", "unknown") if isinstance(value, dict) else "invalid response"
            raise BrigadeError(f"notification was not shown: {reason}")
        return True, None
    except BrigadeError as exc:
        append_event("transport_error", transport="notify", error=str(exc))
        return False, str(exc)


def prompt(pane_id: str, message: str) -> tuple[bool, str | None]:
    if len(message.splitlines()) > 3 or "`" in message:
        return False, "internal safety check rejected an unsafe or overlong prompt"
    try:
        herdr_json("agent", "prompt", pane_id, message)
        return True, None
    except BrigadeError as exc:
        append_event("transport_error", transport="prompt", pane_id=pane_id, error=str(exc))
        return False, str(exc)


def _record_repo_state(repo: pathlib.Path, **fields: Any) -> None:
    with state_transaction() as state:
        current = state["repos"].setdefault(os.fspath(repo), {})
        current.update({"updated_at": utc_now(), **fields})
        if fields.get("verdict") in {"error", "changed", "fail", "timeout"}:
            current.pop("verified_fingerprint", None)


def competing_working_pane(repo: pathlib.Path, pane_id: str | None, state: Mapping[str, Any]) -> str | None:
    if pane_id is None:
        return None
    for pane in list_agents():
        other_id = pane.get("pane_id")
        if other_id == pane_id or pane.get("agent_status") != "working":
            continue
        other_repo = resolve_pane_repo(pane, state)
        if other_repo == repo:
            return str(other_id)
    return None


def validate_feedback_target(pane_id: str, repo: pathlib.Path, expected_identity: Mapping[str, Any]) -> tuple[bool, str]:
    try:
        pane = next((item for item in list_agents() if item.get("pane_id") == pane_id), {})
        if pane.get("agent_status") != "done":
            return False, f"pane status is {pane.get('agent_status', 'unknown')}, not done"
        identity = {key: value for key, value in expected_identity.items() if key != "state_change_seq"}
        if not same_identity(identity, pane):
            return False, "pane identity changed"
        if expected_identity.get("state_change_seq") is None or pane.get("state_change_seq") != expected_identity["state_change_seq"]:
            return False, "agent turn changed during verification"
        current_repo = resolve_pane_repo(pane, load_state())
        if current_repo != repo:
            return False, "pane binding or repository changed"
        return True, ""
    except BrigadeError as exc:
        return False, str(exc)


def feedback_allowed(state: dict[str, Any], pane_id: str, repo: pathlib.Path, fingerprint_value: str) -> bool:
    scope = session_scope() or "no-session"
    key = hashlib.sha256(os.fsencode(f"{scope}\0{pane_id}\0{repo}")).hexdigest()
    feedback = state.setdefault("feedback", {})
    item = feedback.get(key)
    if not isinstance(item, dict):
        feedback[key] = {"last_fingerprint": None, "count": 0, "updated_at": utc_now()}
        item = feedback[key]
    # A given failed snapshot is fed back once. Across edited retry snapshots,
    # at most two prompts are sent before a successful verification resets it.
    return (
        item.get("last_fingerprint") != fingerprint_value
        and int(item.get("count", 0)) < MAX_FEEDBACKS_PER_CYCLE
    )


def increment_feedback(state: dict[str, Any], pane_id: str, repo: pathlib.Path, fingerprint_value: str) -> None:
    scope = session_scope() or "no-session"
    key = hashlib.sha256(os.fsencode(f"{scope}\0{pane_id}\0{repo}")).hexdigest()
    feedback = state.setdefault("feedback", {})
    item = feedback.setdefault(key, {"last_fingerprint": None, "count": 0})
    item["last_fingerprint"] = fingerprint_value
    item["count"] = int(item.get("count", 0)) + 1
    item["updated_at"] = utc_now()


def route_result(
    *,
    repo: pathlib.Path,
    pane_id: str | None,
    expected_identity: Mapping[str, Any] | None,
    config: Config,
    verdict: str,
    fingerprint_value: str,
    report: pathlib.Path,
) -> None:
    # A foreground/manual run without --pane is deliberately log-only.
    if pane_id is None:
        return
    if verdict == "pass":
        if config.on_pass == "notify":
            ok, error = notify("Kitchen Brigade passed", f"{repo.name}: all checks passed")
            if not ok:
                append_to_report(report, f"\nNotification delivery failed: {error}\n")
        return
    if config.on_fail == "log":
        return
    if config.on_fail == "notify" or expected_identity is None or verdict in {"changed", "error"}:
        ok, error = notify("Kitchen Brigade failed", f"{repo.name}: {verdict}. Report: {report}")
        if not ok:
            append_to_report(report, f"\nNotification delivery failed: {error}\n")
        return
    safe, reason = validate_feedback_target(pane_id, repo, expected_identity)
    if not safe:
        ok, error = notify("Kitchen Brigade did not prompt", f"{repo.name}: {reason}. Report: {report}")
        append_to_report(report, f"\nPrompt withheld: {reason}\n")
        if not ok:
            append_to_report(report, f"Notification delivery also failed: {error}\n")
        return
    # Reserve the feedback before submission. Herdr acknowledging submission does
    # not prove a turn started; never retry an ambiguous transport failure.
    with state_transaction() as state:
        allowed = feedback_allowed(state, pane_id, repo, fingerprint_value)
        if allowed:
            increment_feedback(state, pane_id, repo, fingerprint_value)
    if not allowed:
        append_to_report(report, "\nPrompt suppressed: feedback limit reached for this pane/repository snapshot.\n")
        append_event("prompt_suppressed", pane_id=pane_id, repo=str(repo), reason="feedback_limit")
        return
    message = f"Kitchen Brigade found verification failures.\nRead {report}\nFix them, rerun the checks, and report done again."
    ok, error = prompt(pane_id, message)
    if not ok:
        append_to_report(report, f"\nPrompt delivery failed: {error}\n")
        notify_ok, notify_error = notify("Kitchen Brigade prompt failed", f"{repo.name}: {error}. Report: {report}")
        if not notify_ok:
            append_to_report(report, f"Notification delivery also failed: {notify_error}\n")


def append_to_report(path: pathlib.Path, text: str) -> None:
    try:
        current = path.stat().st_size
        data = text.encode("utf-8", "replace")
        if current >= MAX_REPORT_BYTES:
            return
        with path.open("ab") as out:
            out.write(data[: MAX_REPORT_BYTES - current])
    except OSError as exc:
        append_event("report_append_error", report=str(path), error=str(exc))


def write_error_report(repo: pathlib.Path, reason: str, pane_id: str | None = None) -> pathlib.Path:
    path = report_path(repo, "error")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as report:
        report.write(f"Kitchen Brigade error\nTime: {utc_now()}\nRepository: {repo}\nPane: {pane_id or '-'}\n\n{reason}\n")
    _record_repo_state(repo, verdict="error", report=str(path), pane_id=pane_id, reason=reason)
    append_event("error", repo=str(repo), pane_id=pane_id, reason=reason, report=str(path))
    print(f"Kitchen Brigade error: {reason}. Report: {path}", file=sys.stderr)
    return path


def verify(repo: pathlib.Path, pane_id: str | None, force: bool, automatic: bool) -> int:
    repo = repo_root(repo)
    prune_reports()
    expected_identity: dict[str, Any] | None = None
    if pane_id:
        try:
            initial = next((item for item in list_agents() if item.get("pane_id") == pane_id), None)
            if initial is None:
                raise BrigadeError(f"agent pane {pane_id} was not found")
            expected_identity = {**pane_identity(initial), "state_change_seq": initial.get("state_change_seq")}
        except BrigadeError as exc:
            path = write_error_report(repo, str(exc), pane_id)
            notify("Kitchen Brigade error", f"{repo.name}: {exc}. Report: {path}")
            return 2
    try:
        with verification_locks(repo):
            try:
                config = load_config(repo)
            except ConfigError as exc:
                path = write_error_report(repo, str(exc), pane_id)
                if pane_id:
                    notify("Kitchen Brigade config error", f"{repo.name}: {exc}. Report: {path}")
                return 2
            if config is None:
                append_event("skip", repo=str(repo), pane_id=pane_id, reason="no_config")
                if not automatic:
                    print(f"Kitchen Brigade: {repo} has no {CONFIG_NAME}; nothing to run.")
                return 0
            state = load_state()
            try:
                competitor = competing_working_pane(repo, pane_id, state)
            except BrigadeError as exc:
                path = write_error_report(repo, f"cannot check competing panes: {exc}", pane_id)
                notify("Kitchen Brigade error", f"{repo.name}: could not check competing panes. Report: {path}")
                return 2
            if competitor:
                reason = f"pane {competitor} is still working in the same repository"
                append_event("skip", repo=str(repo), pane_id=pane_id, reason="competing_pane", competing_pane=competitor)
                notify("Kitchen Brigade skipped", f"{repo.name}: {reason}")
                _record_repo_state(repo, verdict="skipped", reason=reason, pane_id=pane_id)
                return 0
            try:
                before = fingerprint(repo)
                if load_config(repo) != config:
                    raise BrigadeError("configuration changed before verification could start")
            except BrigadeError as exc:
                path = write_error_report(repo, f"fingerprint failed: {exc}", pane_id)
                if pane_id:
                    notify("Kitchen Brigade error", f"{repo.name}: fingerprint failed. Report: {path}")
                return 2
            prior = state.get("repos", {}).get(str(repo), {})
            if not force and isinstance(prior, dict) and prior.get("verified_fingerprint") == before:
                append_event("skip", repo=str(repo), pane_id=pane_id, reason="unchanged", fingerprint=before)
                _record_repo_state(repo, verdict="skipped", reason="unchanged", pane_id=pane_id, fingerprint=before)
                return 0
            if automatic and not force and prior.get("fingerprint") == before and prior.get("last_result") in {"fail", "timeout"}:
                # Only suppress the same pane's duplicate failed snapshot. A
                # different agent still needs its own evidence and feedback.
                feedback = load_state()
                if not feedback_allowed(feedback, pane_id or "", repo, before):
                    append_event("skip", repo=str(repo), pane_id=pane_id, reason="unchanged_failure")
                    return 0

            path = report_path(repo, "running")
            results: list[CommandResult] = []
            deadline = time.monotonic() + config.timeout_seconds
            _record_repo_state(repo, verdict="running", pane_id=pane_id, report=str(path), reason="checks in progress")
            with os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600), "w+b") as raw:
                writer = ReportWriter(raw)
                writer.write(
                    f"Kitchen Brigade verification\nStarted: {utc_now()}\nRepository: {repo}\n"
                    f"Pane: {pane_id or '-'}\nFingerprint before: {before}\n"
                    f"Timeout: {config.timeout_seconds:g}s (whole suite)\n"
                )
                for index, command in enumerate(config.commands, 1):
                    writer.write(f"\n[{index}/{len(config.commands)}] $ {command}\n")
                    result = run_command(command, repo, deadline, writer)
                    results.append(result)
                    if result.timed_out:
                        writer.write(f"\nTIMED OUT after {result.duration_seconds:.2f}s\n")
                        break
                    writer.write(f"\nExit: {result.exit_code} ({result.duration_seconds:.2f}s)\n")
                    if result.exit_code != 0:
                        break
                try:
                    after = fingerprint(repo)
                except BrigadeError as exc:
                    after = f"error:{exc}"
                changed = after != before
                failed = next((item for item in results if item.timed_out or item.exit_code != 0), None)
                if changed:
                    verdict = "changed"
                    exit_code = 2
                    writer.write(f"\nREFUSED: repository changed during verification.\nFingerprint after: {after}\n")
                elif failed is not None:
                    verdict = "timeout" if failed.timed_out else ("error" if failed.exit_code in {126, 127} else "fail")
                    exit_code = 2 if verdict == "error" else 1
                    writer.write(f"\nVerdict: {verdict.upper()}\n")
                elif len(results) != len(config.commands):
                    verdict = "error"
                    exit_code = 2
                    writer.write("\nVerdict: ERROR (suite did not complete)\n")
                else:
                    verdict = "pass"
                    exit_code = 0
                    writer.write("\nVerdict: PASS\n")
                writer.finish()
            final_path = path.with_name(path.name.replace("-running.txt", f"-{verdict}.txt"))
            os.replace(path, final_path)
            with state_transaction() as state:
                repo_state = state["repos"].setdefault(str(repo), {})
                repo_state.update(
                    updated_at=utc_now(), last_run_at=utc_now(), verdict=verdict,
                    last_result=verdict, fingerprint=before, pane_id=pane_id, report=str(final_path),
                )
                repo_state.pop("reason", None)
                repo_state.pop("verified_fingerprint", None)
                if verdict == "pass":
                    repo_state["verified_fingerprint"] = before
                    scope = session_scope() or "no-session"
                    if pane_id:
                        feedback_key = hashlib.sha256(os.fsencode(f"{scope}\0{pane_id}\0{repo}")).hexdigest()
                        state["feedback"].pop(feedback_key, None)
            append_event("verdict", repo=str(repo), pane_id=pane_id, verdict=verdict, report=str(final_path), fingerprint=before)
            route_result(
                repo=repo,
                pane_id=pane_id,
                expected_identity=expected_identity,
                config=config,
                verdict=verdict,
                fingerprint_value=before,
                report=final_path,
            )
            if not automatic:
                print(f"Kitchen Brigade: {verdict}. Report: {final_path}")
            return exit_code
    except BusyError as exc:
        append_event("skip", repo=str(repo), pane_id=pane_id, reason="busy")
        if not automatic:
            print(f"Kitchen Brigade: skipped; {exc}.")
        return 0


def detached_worker(pane_id: str, force: bool = False, automatic: bool = False) -> None:
    root = ensure_state_dirs()
    crash = root / "worker-crashes.log"
    args = [sys.executable, os.fspath(plugin_root() / "brigade.py"), "_worker", "--pane", pane_id]
    if force:
        args.append("--force")
    if automatic:
        args.append("--automatic")
    if crash.exists() and crash.stat().st_size >= MAX_AUDIT_BYTES:
        os.replace(crash, root / "worker-crashes.previous.log")
    with crash.open("ab") as errors:
        subprocess.Popen(
            args,
            cwd=plugin_root(),
            stdin=subprocess.DEVNULL,
            stdout=errors,
            stderr=errors,
            start_new_session=True,
            close_fds=True,
        )
    append_event("dispatched", pane_id=pane_id, force=force)


def worker(pane_id: str, force: bool, automatic: bool = False) -> int:
    with contextlib.suppress(OSError):
        os.nice(19)
    try:
        pane = pane_info(pane_id)
        if automatic and pane.get("agent_status") != "done":
            append_event("skip", pane_id=pane_id, reason="pane_no_longer_done")
            return 0
        state = load_state()
        repo = resolve_pane_repo(pane, state)
        if repo is None:
            append_event("skip", pane_id=pane_id, reason="no_repository_or_binding")
            return 0
        return verify(repo, pane_id, force, automatic=automatic)
    except BaseException as exc:
        detail = "".join(traceback.format_exception(exc))[-8000:]
        append_event("worker_crash", pane_id=pane_id, error=str(exc))
        crash_path = ensure_state_dirs() / "worker-crashes.log"
        try:
            with crash_path.open("a", encoding="utf-8") as out:
                out.write(f"\n[{utc_now()}] pane={pane_id}\n{detail}\n")
        except OSError:
            pass
        ok, error = notify("Kitchen Brigade crashed", f"Worker for pane {pane_id} crashed. Log: {crash_path}")
        if not ok:
            append_event("transport_error", transport="crash_notify", error=error)
        return 2


def command_event() -> int:
    """Fast, crash-contained Herdr event entry point."""
    try:
        raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON", "")
        value = json.loads(raw)
        data = value.get("data", value) if isinstance(value, dict) else None
        if not isinstance(data, dict):
            raise BrigadeError("event payload has no data object")
        pane_id = data.get("pane_id")
        status = data.get("agent_status")
        if status != "done":
            append_event("event_ignored", pane_id=pane_id, agent_status=status)
            return 0
        if not isinstance(pane_id, str) or not pane_id:
            raise BrigadeError("done event has no pane_id")
        detached_worker(pane_id, automatic=True)
    except BaseException as exc:
        append_event("event_crash", error=str(exc), traceback="".join(traceback.format_exception(exc))[-8000:])
    # Event hooks intentionally exit zero; the durable audit contains malformed payloads.
    return 0


def command_bind(pane_id: str, path: str) -> int:
    scope = session_scope()
    if not scope:
        raise BrigadeError("HERDR_SOCKET_PATH is required so a binding cannot cross sessions")
    repo = repo_root(path)
    pane = pane_info(pane_id)
    identity = pane_identity(pane)
    if not identity.get("terminal_id"):
        raise BrigadeError("pane has no terminal_id; refusing an unsafe binding")
    with state_transaction() as state:
        state["bindings"][binding_key(scope, pane_id)] = {
            "pane_id": pane_id,
            "scope": scope,
            "repo": str(repo),
            "identity": identity,
            "bound_at": utc_now(),
        }
    append_event("bound", pane_id=pane_id, repo=str(repo), scope=scope)
    print(f"Bound {pane_id} to {repo}")
    return 0


def command_unbind(pane_id: str) -> int:
    scope = session_scope()
    if not scope:
        raise BrigadeError("HERDR_SOCKET_PATH is required so the correct session binding is removed")
    with state_transaction() as state:
        removed = state["bindings"].pop(binding_key(scope, pane_id), None)
    append_event("unbound", pane_id=pane_id, existed=bool(removed), scope=scope)
    print(f"Unbound {pane_id}" if removed else f"No binding existed for {pane_id}")
    return 0


def status_data() -> dict[str, Any]:
    state = load_state()
    return {
        "plugin": PLUGIN_ID,
        "state_dir": str(state_dir()),
        "session_scope": session_scope(),
        "repos": state.get("repos", {}),
        "bindings": state.get("bindings", {}),
    }


def command_status(as_json: bool) -> int:
    value = status_data()
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
        return 0
    repos = value["repos"]
    if not repos:
        print("Kitchen Brigade: no verification runs recorded.")
    else:
        print("Kitchen Brigade status")
        for repo, item in sorted(repos.items()):
            print(f"{item.get('verdict', 'unknown'):8} {repo}")
            if item.get("last_result"):
                print(f"         Last check: {item['last_result']} at {item.get('last_run_at', '?')}")
            if item.get("reason"):
                print(f"         {item['reason']}")
            if item.get("report"):
                print(f"         {item['report']}")
    return 0


def command_board() -> int:
    command_status(False)
    print()
    command_doctor()
    print("\nThis popup is read-only. Press q or Ctrl-C to close.")
    if sys.stdin.isatty():
        original = termios.tcgetattr(sys.stdin.fileno())
        try:
            tty.setcbreak(sys.stdin.fileno())
            while True:
                if sys.stdin.read(1).lower() in {"q", "\x1b", ""}:
                    break
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, original)
    return 0


def command_doctor() -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Python", sys.version_info >= (3, 11), sys.version.split()[0]))
    checks.append(("State directory", True, str(ensure_state_dirs())))
    binary = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    checks.append(("HERDR_BIN_PATH", bool(binary and os.access(binary, os.X_OK)), binary or "not set"))
    checks.append(("HERDR_SOCKET_PATH", bool(os.environ.get("HERDR_SOCKET_PATH")), os.environ.get("HERDR_SOCKET_PATH", "not set")))
    if binary and os.access(binary, os.X_OK):
        try:
            value = herdr_json("agent", "list")
            checks.append(("Herdr connection", isinstance(value, dict), "agent list succeeded"))
        except BrigadeError as exc:
            checks.append(("Herdr connection", False, str(exc)))
    failed = False
    for name, ok, detail in checks:
        print(f"{'ok' if ok else 'FAIL':4} {name}: {detail}")
        failed |= not ok
    return 2 if failed else 0


def command_action_status() -> int:
    args = [herdr_bin(), "plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "board", "--focus"]
    pane_id = context_pane_id()
    if pane_id:
        args.extend(["--target-pane", pane_id])
    result = subprocess.run(args, stdin=subprocess.DEVNULL, check=False)
    return 0 if result.returncode == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brigade.py", description="Verify repo-owned checks when a Herdr agent finishes")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("event", help="handle a Herdr status event")
    run = sub.add_parser("run", help="run verification in the foreground")
    run.add_argument("--repo", required=True)
    run.add_argument("--pane")
    run.add_argument("--force", action="store_true")
    check = sub.add_parser("check", help="dispatch verification for a pane")
    check.add_argument("--pane", required=True)
    check.add_argument("--force", action="store_true")
    internal = sub.add_parser("_worker", help=argparse.SUPPRESS)
    internal.add_argument("--pane", required=True)
    internal.add_argument("--force", action="store_true")
    internal.add_argument("--automatic", action="store_true")
    bind = sub.add_parser("bind", help="bind a pane to a repository in this Herdr session")
    bind.add_argument("--pane", required=True)
    bind.add_argument("--repo", required=True)
    unbind = sub.add_parser("unbind", help="remove this session's pane binding")
    unbind.add_argument("--pane", required=True)
    status = sub.add_parser("status", help="show recorded verdicts")
    status.add_argument("--json", action="store_true")
    sub.add_parser("doctor", help="check local runtime health")
    sub.add_parser("board", help="show the read-only status popup")
    sub.add_parser("action-run", help="run the expedite action")
    sub.add_parser("action-status", help="open the status popup")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    def interrupted(signum: int, _frame: Any) -> None:
        raise BrigadeError(f"runner interrupted by {signal.Signals(signum).name}")

    signal.signal(signal.SIGTERM, interrupted)
    args = build_parser().parse_args(argv)
    try:
        if args.command == "event":
            return command_event()
        if args.command == "run":
            return verify(pathlib.Path(args.repo), args.pane, args.force, automatic=False)
        if args.command == "check":
            detached_worker(args.pane, args.force)
            return 0
        if args.command == "_worker":
            return worker(args.pane, args.force, args.automatic)
        if args.command == "bind":
            return command_bind(args.pane, args.repo)
        if args.command == "unbind":
            return command_unbind(args.pane)
        if args.command == "status":
            return command_status(args.json)
        if args.command == "doctor":
            return command_doctor()
        if args.command == "board":
            return command_board()
        if args.command == "action-run":
            pane_id = context_pane_id()
            if not pane_id:
                raise BrigadeError("the expedite action requires a pane context")
            detached_worker(pane_id, True)
            return 0
        if args.command == "action-status":
            return command_action_status()
        raise BrigadeError(f"unknown command: {args.command}")
    except (BrigadeError, OSError, KeyboardInterrupt) as exc:
        print(f"Kitchen Brigade: {exc}", file=sys.stderr)
        append_event("command_error", command=args.command, error=str(exc))
        if args.command == "run":
            try:
                path = write_error_report(pathlib.Path(args.repo).resolve(), str(exc), args.pane)
                if args.pane:
                    notify("Kitchen Brigade error", f"{exc}. Report: {path}")
            except (BrigadeError, OSError) as report_error:
                print(f"Could not save error report: {report_error}", file=sys.stderr)
        return 2
