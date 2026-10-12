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
import re
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
SCRATCH_DIR = ".kitchen-brigade"
SUMMARY_NAME = "last-failure.txt"
MAX_SUMMARY_LINES = 60
MAX_SNAPSHOT_FILES = 20000
MAX_LISTED_PATHS = 12
MAX_REPORT_SCAN_BYTES = 512 * 1024
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
    stop_on_first_failure: bool = True


@dataclass(frozen=True)
class Snapshot:
    digest: str
    files: dict[str, str]
    undecodable_paths: int = 0
    total_paths: int = 0


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    tail: tuple[str, ...] = ()


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
    (root / "snapshots").mkdir(exist_ok=True, mode=0o700)
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
        # Added after 0.1.0; a state file written by an earlier build has no registry.
        value.setdefault("watched", {})
        for field in ("repos", "bindings", "feedback", "watched"):
            entries = value.get(field)
            if not isinstance(entries, dict) or any(not isinstance(item, dict) for item in entries.values()):
                raise ValueError(f"invalid {field} state")
        return value
    except FileNotFoundError:
        return {"version": STATE_VERSION, "repos": {}, "bindings": {}, "feedback": {}, "watched": {}}
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
    if set(value) - {"verify", "report"} or set(verify) - {"commands", "timeout_seconds", "stop_on_first_failure"}:
        raise ConfigError(
            "unknown configuration key; expected [verify] commands/timeout_seconds/stop_on_first_failure and [report]"
        )
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
    stop = verify.get("stop_on_first_failure", True)
    if not isinstance(stop, bool):
        raise ConfigError("verify.stop_on_first_failure must be true or false")
    return Config(tuple(commands), float(timeout), on_fail, on_pass, stop)


def _file_chunk(root: pathlib.Path, relative: bytes) -> bytes:
    """Everything one path contributes to the snapshot digest, as a single value.

    Returning the bytes rather than feeding a shared hasher lets the same walk
    produce both the aggregate digest and a per-file digest, which is what makes
    it possible to say which files an agent actually changed.  The concatenation
    is byte-identical to the previous streaming form, so digests recorded by
    earlier versions stay valid.
    """
    full = os.path.join(os.fsencode(root), relative)
    try:
        metadata = os.lstat(full)
    except FileNotFoundError:
        return b"missing\0"
    prefix = str(metadata.st_mode).encode("ascii") + b"\0"
    if os.path.islink(full):
        return prefix + b"link\0" + os.fsencode(os.readlink(full)) + b"\0"
    if stat.S_ISDIR(metadata.st_mode):
        # Git represents submodules and nested repositories by a directory entry.
        nested = pathlib.Path(os.fsdecode(full))
        if repo_root(nested) != nested.resolve():
            raise BrigadeError(f"cannot fingerprint tracked directory: {nested}")
        return prefix + b"repository\0" + fingerprint(nested).encode("ascii")
    if not stat.S_ISREG(metadata.st_mode):
        return prefix + b"special\0"
    contents = hashlib.sha256()
    with open(full, "rb", buffering=0) as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            contents.update(chunk)
    return prefix + contents.digest()


def capture_snapshot(repo: pathlib.Path) -> Snapshot:
    """Hash HEAD, index entries, and every tracked/untracked working-tree file.

    Git output is kept as bytes and NUL-delimited, so unusual and non-UTF-8 paths
    are unambiguous.  The config is added even if a repository ignores dotfiles.
    The per-file digests are kept as well as the aggregate: the walk already
    computed them, and discarding them is what left the plugin unable to say
    which files an agent touched.
    """
    head = git(repo, "rev-parse", "--verify", "HEAD", check=False)
    head_value = head.stdout.rstrip(b"\n") if head.returncode == 0 else b"<unborn>"
    index = git(repo, "ls-files", "-s", "-z").stdout
    names = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
    # The plugin's own in-repo scratch directory must never be part of a snapshot,
    # or writing the agent's summary into it would invalidate the tree it describes.
    scratch = os.fsencode(SCRATCH_DIR + "/")
    paths = {item for item in names.split(b"\0") if item and not item.startswith(scratch)}
    config_bytes = os.fsencode(CONFIG_NAME)
    if os.path.lexists(os.path.join(os.fsencode(repo), config_bytes)):
        paths.add(config_bytes)
    hasher = hashlib.sha256()
    hasher.update(b"kitchen-brigade-fingerprint-v3\0HEAD\0" + head_value + b"\0INDEX\0" + index)
    files: dict[str, str] = {}
    undecodable = 0
    for relative in sorted(paths):
        chunk = _file_chunk(repo, relative)
        hasher.update(b"PATH\0" + relative + b"\0")
        hasher.update(chunk)
        hasher.update(b"\0")
        try:
            key = relative.decode("utf-8")
        except UnicodeDecodeError:
            # It still counts toward the digest, so change detection is unaffected;
            # only the per-file attribution list cannot name it.
            undecodable += 1
            continue
        if len(files) < MAX_SNAPSHOT_FILES:
            files[key] = hashlib.sha256(chunk).hexdigest()[:16]
    return Snapshot(hasher.hexdigest(), files, undecodable, len(paths))


def fingerprint(repo: pathlib.Path) -> str:
    return capture_snapshot(repo).digest


def snapshot_path(repo: pathlib.Path) -> pathlib.Path:
    digest = hashlib.sha256(os.fsencode(os.fspath(repo))).hexdigest()[:16]
    return ensure_state_dirs() / "snapshots" / f"{digest}.json"


def save_snapshot(repo: pathlib.Path, snapshot: Snapshot) -> None:
    """Kept out of state.json: this map is large and must not be rewritten on every transaction."""
    try:
        atomic_write_json(snapshot_path(repo), {
            "repo": str(repo),
            "fingerprint": snapshot.digest,
            "captured_at": utc_now(),
            "truncated": len(snapshot.files) < snapshot.total_paths - snapshot.undecodable_paths,
            "files": snapshot.files,
        })
    except OSError as exc:
        append_event("snapshot_error", repo=str(repo), error=str(exc))


def load_snapshot(repo: pathlib.Path) -> Snapshot | None:
    try:
        value = json.loads(snapshot_path(repo).read_text(encoding="utf-8"))
        files = value["files"]
        if not isinstance(files, dict) or value.get("truncated"):
            return None
        return Snapshot(str(value["fingerprint"]), {str(k): str(v) for k, v in files.items()})
    except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def changed_since(previous: Snapshot | None, current: Snapshot) -> list[str] | None:
    """Paths added, modified, or removed since the plugin last looked.

    None means there is no earlier snapshot to compare against, which is not the
    same as nothing having changed and must not be reported as such.
    """
    if previous is None:
        return None
    names = set(previous.files) | set(current.files)
    return sorted(name for name in names if previous.files.get(name) != current.files.get(name))


PATH_TOKEN = re.compile(r"[A-Za-z0-9_./@+-]*[A-Za-z0-9_-]\.[A-Za-z0-9]{1,8}")


def cited_changed_paths(text: str, repo: pathlib.Path, changed: Sequence[str]) -> list[str]:
    """Which of the changed files this failure actually names.

    Checkers print paths, so intersecting the paths in the output with the paths
    the agent touched turns "lint failed" into "lint failed in code you wrote".
    A bare filename only counts when exactly one changed path has that name,
    because guessing between two would be worse than saying nothing.
    """
    if not changed:
        return []
    by_path = set(changed)
    by_base: dict[str, list[str]] = {}
    for item in changed:
        by_base.setdefault(item.rsplit("/", 1)[-1], []).append(item)
    prefix = str(repo) + "/"
    hits: list[str] = []
    seen: set[str] = set()
    for match in PATH_TOKEN.finditer(text):
        token = match.group(0)
        if token.startswith(prefix):
            token = token[len(prefix):]
        token = token.removeprefix("./")
        if token in by_path:
            candidate = token
        else:
            options = by_base.get(token.rsplit("/", 1)[-1], ())
            candidate = options[0] if len(options) == 1 else None
        if candidate and candidate not in seen:
            seen.add(candidate)
            hits.append(candidate)
            if len(hits) >= MAX_LISTED_PATHS:
                break
    return hits


def command_output(report: pathlib.Path, position: int, total: int, command: str) -> str:
    """Just one command's output, sliced out of the report.

    Scanning the whole report would match the plugin's own header, which lists the
    changed paths, and the echoed command lines: attribution would then confirm
    itself from its own output rather than from what the checker said.
    """
    try:
        with report.open("rb") as stream:
            text = stream.read(MAX_REPORT_SCAN_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""
    marker = f"[{position}/{total}] $ {command}\n"
    start = text.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    for terminator in ("\nExit: ", "\nTIMED OUT"):
        end = text.find(terminator, start)
        if end >= 0:
            return text[start:end]
    return text[start:]


def watched_candidates() -> tuple[list[tuple[pathlib.Path, str]], list[str]]:
    """Which watched repositories moved since the plugin last looked at them.

    Pane resolution fails for every agent launched outside a repository, which is
    the normal case.  The registry answers the other question instead: of the
    repositories the operator asked us to watch, which one has new work in it.
    A repository that has never been looked at counts as changed, matching the
    rule that the first observed snapshot is always checked.
    """
    state = load_state()
    watched = state.get("watched", {})
    changed: list[tuple[pathlib.Path, str]] = []
    problems: list[str] = []
    for key in sorted(watched):
        entry = watched[key] if isinstance(watched[key], dict) else {}
        repo = pathlib.Path(key)
        if not (repo / CONFIG_NAME).is_file():
            continue
        try:
            current = fingerprint(repo)
        except (BrigadeError, OSError) as exc:
            problems.append(f"{repo}: {exc}")
            continue
        if entry.get("last_seen_fingerprint") != current:
            changed.append((repo, current))
    return changed, problems


def mark_watched_seen(repo: pathlib.Path, value: str) -> None:
    """Record a snapshot as looked at, so the next event does not re-select it.

    Only the repository actually selected is marked.  A changed repository that
    was passed over stays a candidate, so an ambiguous event keeps complaining
    instead of quietly forgetting the work.
    """
    with state_transaction() as state:
        entry = state.setdefault("watched", {}).setdefault(str(repo), {})
        entry["last_seen_fingerprint"] = value
        entry["last_seen_at"] = utc_now()


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
    # A bounded tail of the command's own output, so a three-line message back to
    # the agent can name the actual error instead of only a file path.
    tail: list[bytes] = []
    pending = b""
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
                    pending += data
                    if b"\n" in pending or len(pending) > 65536:
                        *complete, pending = pending.split(b"\n")
                        tail.extend(complete)
                        if len(tail) > 120:
                            del tail[:-60]
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
    if pending:
        tail.append(pending)
    return CommandResult(
        command,
        None if timed_out else process.returncode,
        timed_out,
        time.monotonic() - started,
        tuple(line.decode("utf-8", "replace")[:400] for line in tail[-30:]),
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


def validate_feedback_target(pane_id: str, repo: pathlib.Path, expected_identity: Mapping[str, Any],
                             inferred: bool = False) -> tuple[bool, str]:
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
        # An inferred repository never resolves from the pane, so equality is only
        # required when the pane does point somewhere of its own.
        if current_repo != repo and not (inferred and current_repo is None):
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


def load_baseline(repo: pathlib.Path) -> dict[str, int]:
    """Exit codes a repository is already known to produce before any agent touched it.

    Most real repositories are not green.  Without this, the first honest run
    accuses every agent of failures it inherited, and two prompts later the gate
    has taught its user to ignore it.
    """
    item = load_state().get("repos", {}).get(str(repo))
    baseline = item.get("baseline") if isinstance(item, dict) else None
    commands = baseline.get("commands") if isinstance(baseline, dict) else None
    if not isinstance(commands, dict):
        return {}
    return {
        key: value for key, value in commands.items()
        if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
    }


def save_baseline(repo: pathlib.Path, results: Sequence[CommandResult], snapshot: str) -> int:
    """Record only the commands that actually failed; a passing command needs no licence."""
    tolerated = {
        item.command: item.exit_code for item in results
        if not item.timed_out and isinstance(item.exit_code, int) and item.exit_code != 0
    }
    with state_transaction() as state:
        repo_state = state["repos"].setdefault(str(repo), {})
        repo_state["baseline"] = {"captured_at": utc_now(), "fingerprint": snapshot, "commands": tolerated}
    return len(tolerated)


def sanitize_for_prompt(text: str) -> str:
    """Herdr input is typed into a live shell, so a backtick must never survive."""
    cleaned = "".join(" " if character < " " or character == "\x7f" else character for character in text)
    return " ".join(cleaned.replace("`", "'").split())


def last_meaningful_line(tail: Sequence[str]) -> str:
    """Checkers put their verdict last: the error count, the failing assertion, the summary."""
    for line in reversed(tail):
        stripped = line.strip()
        if len(stripped) >= 3:
            return stripped
    return ""


def failure_message(results: Sequence[CommandResult], regressions: Sequence[CommandResult],
                    verdict: str, pointer: pathlib.Path) -> str:
    """Three lines, because that is all Herdr will reliably deliver."""
    timed_out = next((item for item in results if item.timed_out), None)
    # The command is trimmed, never the exit code: a truncated head that loses the
    # code tells the agent less than naming no command at all.
    if timed_out is not None:
        head = f"Kitchen Brigade: {sanitize_for_prompt(timed_out.command)[:140]} timed out."
        detail = "It consumed the whole suite budget, so nothing after it ran."
    elif regressions:
        first = regressions[0]
        head = f"Kitchen Brigade: {sanitize_for_prompt(first.command)[:140]} failed, exit {first.exit_code}."
        detail = sanitize_for_prompt(last_meaningful_line(first.tail)) or "The report has the output."
    else:
        head = f"Kitchen Brigade: verification ended as {verdict}."
        detail = "Nothing certified this snapshot."
    return f"{head[:240]}\n{detail[:240]}\nRead {pointer} and fix it before reporting done."


def write_repo_summary(repo: pathlib.Path, text: str) -> pathlib.Path | None:
    """Put a short summary inside the repository, where a sandboxed agent can read it.

    The directory ignores itself, so it never enters a snapshot and the agent's
    own pointer file cannot invalidate the tree it describes.
    """
    directory = repo / SCRATCH_DIR
    try:
        directory.mkdir(exist_ok=True, mode=0o700)
        (directory / ".gitignore").write_text("*\n", encoding="utf-8")
        path = directory / SUMMARY_NAME
        path.write_text(text, encoding="utf-8")
        return path
    except OSError as exc:
        append_event("summary_error", repo=str(repo), error=str(exc))
        return None


def summary_text(repo: pathlib.Path, verdict: str, config: Config, results: Sequence[CommandResult],
                 regressions: Sequence[CommandResult], tolerated: Sequence[CommandResult],
                 report: pathlib.Path, changed: Sequence[str] | None = None,
                 cited: Sequence[str] = (), config_edited: bool = False) -> str:
    lines = [
        "Kitchen Brigade verification summary",
        f"Repository: {repo}",
        f"Time: {utc_now()}",
        f"Verdict: {verdict.upper()}",
        f"Commands: {len(results)} of {len(config.commands)} run",
        "",
    ]
    if config_edited:
        lines += [f"ALERT: {CONFIG_NAME} changed in this snapshot. The checks themselves were edited.", ""]
    tolerated_commands = {item.command for item in tolerated}
    regressed_commands = {item.command for item in regressions}
    for item in results:
        if item.timed_out:
            label = "TIMED OUT"
        elif item.command in regressed_commands:
            label = f"REGRESSED exit {item.exit_code}"
        elif item.command in tolerated_commands:
            label = f"pre-existing exit {item.exit_code}, matches baseline"
        else:
            label = f"ok exit {item.exit_code}"
        lines.append(f"  {label}  {item.command}")
    if changed is None:
        lines += ["", "Changed since the last check: unknown, there is no earlier snapshot."]
    elif not changed:
        lines += ["", "Nothing changed since the last check, so this failure predates your work."]
    else:
        lines += ["", f"Changed since the last check: {len(changed)} path(s)."]
        if cited:
            lines.append(f"This failure names {len(cited)} of them:")
            lines += [f"  {name}" for name in cited[:MAX_LISTED_PATHS]]
        else:
            lines.append("This failure names none of them, so it may not be caused by your changes.")
    focus = next(iter(regressions), None) or next((item for item in results if item.timed_out), None)
    if focus is not None and focus.tail:
        lines += ["", f"Last output from {focus.command}:"]
        room = max(4, MAX_SUMMARY_LINES - len(lines) - 3)
        lines += [f"  {line}" for line in focus.tail[-room:]]
    lines += ["", f"Full output: {report}"]
    return "\n".join(lines) + "\n"


def route_result(
    *,
    repo: pathlib.Path,
    pane_id: str | None,
    expected_identity: Mapping[str, Any] | None,
    config: Config,
    verdict: str,
    fingerprint_value: str,
    report: pathlib.Path,
    notify_only: bool = False,
    inferred: bool = False,
    pointer: pathlib.Path | None = None,
    message: str | None = None,
) -> None:
    # A foreground/manual run without --pane is deliberately log-only.
    if pane_id is None:
        return
    pointer = pointer or report
    if verdict in {"pass", "baseline"}:
        if config.on_pass == "notify":
            body = ("all checks passed" if verdict == "pass"
                    else "no regressions; pre-existing failures match the baseline")
            ok, error = notify("Kitchen Brigade passed", f"{repo.name}: {body}")
            if not ok:
                append_to_report(report, f"\nNotification delivery failed: {error}\n")
        return
    if config.on_fail == "log":
        return
    if config.on_fail == "notify" or notify_only or expected_identity is None or verdict in {"changed", "error"}:
        if notify_only:
            append_to_report(report, "\nPrompt withheld: repository was inferred while another agent was working.\n")
        ok, error = notify("Kitchen Brigade failed", f"{repo.name}: {verdict}. Report: {pointer}")
        if not ok:
            append_to_report(report, f"\nNotification delivery failed: {error}\n")
        return
    safe, reason = validate_feedback_target(pane_id, repo, expected_identity, inferred)
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
    if message is None:
        message = f"Kitchen Brigade found verification failures.\nRead {pointer}\nFix them, rerun the checks, and report done again."
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


def verify(repo: pathlib.Path, pane_id: str | None, force: bool, automatic: bool,
           notify_only: bool = False, inferred: bool = False) -> int:
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
                current_snapshot = capture_snapshot(repo)
                before = current_snapshot.digest
                changed_paths = changed_since(load_snapshot(repo), current_snapshot)
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

            # An agent editing its own gate is the single most interesting thing
            # this plugin can observe.  A foreground run is the operator editing
            # their own config, which is not the same event.
            config_edited = bool(pane_id) and changed_paths is not None and CONFIG_NAME in changed_paths
            path = report_path(repo, "running")
            results: list[CommandResult] = []
            regressions: list[CommandResult] = []
            tolerated: list[CommandResult] = []
            deadline = time.monotonic() + config.timeout_seconds
            _record_repo_state(repo, verdict="running", pane_id=pane_id, report=str(path), reason="checks in progress")
            with os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600), "w+b") as raw:
                writer = ReportWriter(raw)
                writer.write(
                    f"Kitchen Brigade verification\nStarted: {utc_now()}\nRepository: {repo}\n"
                    f"Pane: {pane_id or '-'}\nFingerprint before: {before}\n"
                    f"Timeout: {config.timeout_seconds:g}s (whole suite)\n"
                )
                if changed_paths is None:
                    writer.write("Changed since last check: unknown (no earlier snapshot)\n")
                else:
                    writer.write(f"Changed since last check: {len(changed_paths)} path(s)\n")
                    for name in changed_paths[:MAX_LISTED_PATHS]:
                        writer.write(f"  {name}\n")
                    if len(changed_paths) > MAX_LISTED_PATHS:
                        writer.write(f"  ... and {len(changed_paths) - MAX_LISTED_PATHS} more\n")
                if config_edited:
                    writer.write(f"ALERT: {CONFIG_NAME} changed in this snapshot; the checks themselves were edited.\n")
                baseline = load_baseline(repo)
                if baseline:
                    writer.write(f"Baseline: {len(baseline)} command(s) already failed before this snapshot\n")
                for index, command in enumerate(config.commands, 1):
                    writer.write(f"\n[{index}/{len(config.commands)}] $ {command}\n")
                    result = run_command(command, repo, deadline, writer)
                    results.append(result)
                    if result.timed_out:
                        writer.write(f"\nTIMED OUT after {result.duration_seconds:.2f}s\n")
                        break
                    expected = baseline.get(command)
                    inherited = result.exit_code != 0 and expected is not None and result.exit_code == expected
                    label = "ok" if result.exit_code == 0 else ("PRE-EXISTING" if inherited else "REGRESSED")
                    writer.write(f"\nExit: {result.exit_code} ({result.duration_seconds:.2f}s) {label}\n")
                    if inherited:
                        # Never stop on a failure the agent inherited, or one stale
                        # lint error hides every regression behind it.
                        tolerated.append(result)
                        continue
                    if result.exit_code != 0:
                        regressions.append(result)
                        if config.stop_on_first_failure:
                            break
                try:
                    after = fingerprint(repo)
                except BrigadeError as exc:
                    after = f"error:{exc}"
                changed = after != before
                timed_out = next((item for item in results if item.timed_out), None)
                if changed:
                    verdict = "changed"
                    exit_code = 2
                    writer.write(f"\nREFUSED: repository changed during verification.\nFingerprint after: {after}\n")
                elif timed_out is not None:
                    verdict = "timeout"
                    exit_code = 1
                    writer.write("\nVerdict: TIMEOUT\n")
                elif regressions:
                    verdict = "error" if any(item.exit_code in {126, 127} for item in regressions) else "fail"
                    exit_code = 2 if verdict == "error" else 1
                    writer.write(f"\nVerdict: {verdict.upper()}\n")
                    writer.write("Regressed: " + ", ".join(item.command for item in regressions) + "\n")
                elif len(results) != len(config.commands):
                    verdict = "error"
                    exit_code = 2
                    writer.write("\nVerdict: ERROR (suite did not complete)\n")
                elif tolerated:
                    verdict = "baseline"
                    exit_code = 0
                    writer.write("\nVerdict: BASELINE (no regressions)\n")
                else:
                    verdict = "pass"
                    exit_code = 0
                    writer.write("\nVerdict: PASS\n")
                if tolerated:
                    writer.write("Pre-existing, tolerated: " + ", ".join(item.command for item in tolerated) + "\n")
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
                if verdict in {"pass", "baseline"}:
                    repo_state["verified_fingerprint"] = before
                    scope = session_scope() or "no-session"
                    if pane_id:
                        feedback_key = hashlib.sha256(os.fsencode(f"{scope}\0{pane_id}\0{repo}")).hexdigest()
                        state["feedback"].pop(feedback_key, None)
            # Written only after the post-run fingerprint, so the summary can never
            # be mistaken for the agent changing the tree during verification.
            if verdict != "changed":
                save_snapshot(repo, current_snapshot)
            cited: list[str] = []
            pointer = final_path
            if verdict in {"fail", "timeout", "error"}:
                focus = next(iter(regressions), None) or next((item for item in results if item.timed_out), None)
                position = next((i for i, item in enumerate(results, 1) if item is focus), None)
                if changed_paths and focus is not None and position is not None:
                    cited = cited_changed_paths(
                        command_output(final_path, position, len(config.commands), focus.command),
                        repo, changed_paths,
                    )
                summary = write_repo_summary(
                    repo, summary_text(repo, verdict, config, results, regressions, tolerated,
                                       final_path, changed_paths, cited, config_edited)
                )
                if summary is not None:
                    pointer = summary
            if config_edited:
                append_event("config_edited", repo=str(repo), pane_id=pane_id, verdict=verdict)
                # on_fail = log is an explicit request for silence, so the alarm
                # goes to the report, the audit log and doctor instead, where it
                # cannot be lost but does not break the stated contract.
                if config.on_fail != "log":
                    notify("Kitchen Brigade: the checks were edited",
                           f"{repo.name}: {CONFIG_NAME} changed in the snapshot just verified ({verdict}).")
            append_event("verdict", repo=str(repo), pane_id=pane_id, verdict=verdict,
                         report=str(final_path), fingerprint=before,
                         regressed=len(regressions), tolerated=len(tolerated))
            route_result(
                repo=repo,
                pane_id=pane_id,
                expected_identity=expected_identity,
                config=config,
                verdict=verdict,
                fingerprint_value=before,
                report=final_path,
                notify_only=notify_only,
                inferred=inferred,
                pointer=pointer,
                message=failure_message(results, regressions, verdict, pointer),
            )
            if not automatic:
                print(f"Kitchen Brigade: {verdict}. Report: {pointer}")
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


def infer_repo_for_pane(pane_id: str) -> tuple[pathlib.Path | None, bool]:
    """Pick a watched repository for a pane whose directory told us nothing.

    Returns the repository and whether feedback must be downgraded to a
    notification.  Inference cannot prove which agent did the work, so it only
    types at the finishing pane while no other agent is working.
    """
    candidates, problems = watched_candidates()
    for problem in problems:
        append_event("watch_error", pane_id=pane_id, detail=problem)
    if not candidates:
        # Quiet because nothing moved is healthy; quiet because nothing is watched
        # is the failure doctor must shout about, so they are separate reasons.
        eligible = [key for key in load_state().get("watched", {})
                    if (pathlib.Path(key) / CONFIG_NAME).is_file()]
        append_event("skip", pane_id=pane_id,
                     reason="no_watched_change" if eligible else "no_repository_or_binding")
        return None, False
    if len(candidates) > 1:
        names = ", ".join(repo.name for repo, _ in candidates)
        append_event("skip", pane_id=pane_id, reason="ambiguous_watched",
                     candidates=[str(repo) for repo, _ in candidates])
        notify("Kitchen Brigade could not attribute a check",
               f"{len(candidates)} watched repositories changed ({names}). Run a check with --repo.")
        return None, False
    repo, snapshot = candidates[0]
    mark_watched_seen(repo, snapshot)
    try:
        busy = any(item.get("pane_id") != pane_id and item.get("agent_status") == "working"
                   for item in list_agents())
    except BrigadeError as exc:
        append_event("transport_error", transport="infer_agent_list", error=str(exc))
        busy = True
    append_event("inferred", pane_id=pane_id, repo=str(repo), notify_only=busy)
    return repo, busy


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
        notify_only = inferred = False
        if repo is None:
            repo, notify_only = infer_repo_for_pane(pane_id)
            if repo is None:
                return 0
            inferred = True
        return verify(repo, pane_id, force, automatic=automatic,
                      notify_only=notify_only, inferred=inferred)
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


def recent_events(hours: float = 24.0) -> list[dict[str, Any]]:
    """Read back the audit records this plugin already writes."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    root = ensure_state_dirs()
    found: list[dict[str, Any]] = []
    for name in ("events.previous.jsonl", "events.jsonl"):
        try:
            text = (root / name).read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, OSError):
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                when = dt.datetime.fromisoformat(item["time"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if not isinstance(item, dict):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            if when >= cutoff:
                found.append(item)
    return found


def activity_summary(hours: float = 24.0) -> dict[str, Any]:
    """What the gate actually did, as opposed to whether it is wired up."""
    events = recent_events(hours)
    skips: dict[str, int] = {}
    verdicts: dict[str, int] = {}
    attributed = 0
    for item in events:
        kind = item.get("kind")
        if kind == "skip":
            reason = str(item.get("reason", "unknown"))
            skips[reason] = skips.get(reason, 0) + 1
        elif kind == "verdict":
            value = str(item.get("verdict", "unknown"))
            verdicts[value] = verdicts.get(value, 0) + 1
            # A foreground run carries no pane, so it says nothing about whether
            # the gate is catching agents.  Only pane-attributed verdicts count.
            attributed += bool(item.get("pane_id"))
    return {
        "hours": hours,
        "dispatched": sum(1 for item in events if item.get("kind") == "dispatched"),
        "verified": attributed,
        "verdicts": verdicts,
        "manual": sum(verdicts.values()) - attributed,
        "skipped": sum(skips.values()),
        "skips": skips,
        "inferred": sum(1 for item in events if item.get("kind") == "inferred"),
        "config_edited": sum(1 for item in events if item.get("kind") == "config_edited"),
        "crashes": sum(1 for item in events if item.get("kind") in {"worker_crash", "event_crash"}),
    }


def command_watch(repo: str | None, root: str | None, as_list: bool) -> int:
    """Register repositories the gate may check when a pane resolves to nothing."""
    if as_list:
        watched = load_state().get("watched", {})
        if not watched:
            print("Kitchen Brigade: no watched repositories.")
            return 0
        for key in sorted(watched):
            marker = "ok" if (pathlib.Path(key) / CONFIG_NAME).is_file() else "no config"
            print(f"{marker:9} {key}")
        return 0
    if not repo and not root:
        raise BrigadeError("watch needs --repo, --root, or --list")
    found: list[pathlib.Path] = []
    if repo:
        found.append(repo_root(repo))
    if root:
        base = pathlib.Path(root).expanduser()
        if not base.is_dir():
            raise BrigadeError(f"not a directory: {base}")
        # A scan only adopts repositories that already opted in, so pointing at a
        # projects directory cannot silently enrol everything under it.
        for child in sorted(base.iterdir()):
            if not child.is_dir() or not (child / CONFIG_NAME).is_file():
                continue
            try:
                resolved = repo_root(child)
            except BrigadeError:
                continue
            if resolved == child.resolve() and resolved not in found:
                found.append(resolved)
    if not found:
        print(f"Kitchen Brigade: found no repository carrying {CONFIG_NAME}.")
        return 0
    # Seed each new entry with its current snapshot.  The registry answers which
    # repository has new work, and at registration nothing is new; without this,
    # registering several repositories makes the next completion ambiguous by
    # construction and no check runs at all.
    seeds: dict[str, str] = {}
    for item in found:
        if not (item / CONFIG_NAME).is_file():
            continue
        try:
            captured = capture_snapshot(item)
        except (BrigadeError, OSError) as exc:
            append_event("watch_error", repo=str(item), detail=str(exc))
            continue
        seeds[str(item)] = captured.digest
        if load_snapshot(item) is None:
            save_snapshot(item, captured)
    fresh: list[pathlib.Path] = []
    with state_transaction() as state:
        watched = state.setdefault("watched", {})
        for item in found:
            entry = watched.setdefault(str(item), {})
            entry.setdefault("added_at", utc_now())
            # Seeding is also the repair path: an entry with no snapshot is a
            # permanent candidate, and several of those make every completion
            # ambiguous.  Re-running watch is the idempotent way to fix that.
            if entry.get("last_seen_fingerprint") is None and str(item) in seeds:
                fresh.append(item)
                entry["last_seen_fingerprint"] = seeds[str(item)]
                entry["last_seen_at"] = utc_now()
            entry["source"] = "root" if root else "repo"
    for item in found:
        note = "" if (item / CONFIG_NAME).is_file() else f"  (no {CONFIG_NAME} yet, so it will be skipped)"
        print(f"Watching {item}{note}")
        append_event("watch_added", repo=str(item), seeded=str(item) in seeds)
    if fresh:
        print("Checks start at the next change. For a verdict on the current tree now, use accept or run --repo.")
    return 0


def command_accept(path: str) -> int:
    """Record what this repository already fails, so later runs only report regressions.

    Every command runs regardless of its exit code: a baseline that stopped at the
    first failure would licence nothing beyond it.
    """
    repo = repo_root(path)
    config = load_config(repo)
    if config is None:
        raise BrigadeError(f"{repo} has no {CONFIG_NAME}")
    results: list[CommandResult] = []
    with verification_locks(repo):
        captured = capture_snapshot(repo)
        snapshot = captured.digest
        save_snapshot(repo, captured)
        deadline = time.monotonic() + config.timeout_seconds
        report = report_path(repo, "baseline")
        with os.fdopen(os.open(report, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600), "w+b") as raw:
            writer = ReportWriter(raw)
            writer.write(
                f"Kitchen Brigade baseline capture\nTime: {utc_now()}\nRepository: {repo}\n"
                f"Fingerprint: {snapshot}\nTimeout: {config.timeout_seconds:g}s (whole suite)\n"
            )
            for index, command in enumerate(config.commands, 1):
                writer.write(f"\n[{index}/{len(config.commands)}] $ {command}\n")
                result = run_command(command, repo, deadline, writer)
                results.append(result)
                if result.timed_out:
                    writer.write(f"\nTIMED OUT after {result.duration_seconds:.2f}s\n")
                    break
                writer.write(f"\nExit: {result.exit_code} ({result.duration_seconds:.2f}s)\n")
            writer.finish()
    if any(item.timed_out for item in results) or len(results) != len(config.commands):
        raise BrigadeError(f"the suite did not complete, so no baseline was recorded. Report: {report}")
    count = save_baseline(repo, results, snapshot)
    # Capturing a baseline is itself a look at this snapshot, so a watched
    # repository must not be re-selected for the same tree afterwards.
    if str(repo) in load_state().get("watched", {}):
        mark_watched_seen(repo, snapshot)
    append_event("baseline_captured", repo=str(repo), tolerated=count, fingerprint=snapshot)
    print(f"Kitchen Brigade: baseline recorded for {repo}.")
    if count:
        print(f"{count} command(s) may keep failing with the same exit code; anything else is a regression:")
        for item in results:
            if item.exit_code != 0:
                print(f"  exit {item.exit_code}  {item.command}")
    else:
        print("Every command passed, so nothing is tolerated and any failure is a regression.")
    print(f"Capture report: {report}")
    return 0


def command_unwatch(repo: str) -> int:
    target = str(repo_root(repo))
    with state_transaction() as state:
        removed = state.setdefault("watched", {}).pop(target, None)
    append_event("watch_removed", repo=target, existed=bool(removed))
    print(f"Stopped watching {target}" if removed else f"Was not watching {target}")
    return 0


def status_data() -> dict[str, Any]:
    state = load_state()
    return {
        "plugin": PLUGIN_ID,
        "state_dir": str(state_dir()),
        "session_scope": session_scope(),
        "repos": state.get("repos", {}),
        "bindings": state.get("bindings", {}),
        "watched": state.get("watched", {}),
        "activity_24h": activity_summary(),
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


def outcome_checks() -> list[tuple[str, bool, str]]:
    """Health is what the gate produced, not only what it is connected to.

    A gate that receives every event and verifies nothing looks identical to a
    healthy one from the wiring checks alone, which is how this plugin spent its
    first day dead while reporting ok on every line.
    """
    checks: list[tuple[str, bool, str]] = []
    activity = activity_summary()
    dispatched, verified = activity["dispatched"], activity["verified"]
    checks.append((
        "Done events (24h)",
        not (dispatched and not verified),
        f"{dispatched} dispatched, {verified} verified, {activity['skipped']} skipped",
    ))
    if activity["skips"]:
        ranked = sorted(activity["skips"].items(), key=lambda item: (-item[1], item[0]))
        summary = ", ".join(f"{reason} x{count}" for reason, count in ranked[:3])
        unresolved = activity["skips"].get("no_repository_or_binding", 0)
        checks.append(("Skip reasons (24h)", not (unresolved and not verified), summary))
    checks.append(("Crashes (24h)", activity["crashes"] == 0, str(activity["crashes"])))
    edited = activity["config_edited"]
    checks.append((
        "Checks edited by an agent (24h)",
        edited == 0,
        "none" if not edited else f"{edited} verification(s) ran on a snapshot that changed {CONFIG_NAME}",
    ))
    try:
        state = load_state()
    except BrigadeError as exc:
        checks.append(("Watched repositories", False, str(exc)))
        return checks
    watched = state.get("watched", {})
    repos = state.get("repos", {})
    checks.append(("Watched repositories", bool(watched) or bool(state.get("bindings")),
                   f"{len(watched)} watched; run watch --root to register some" if not watched
                   else f"{len(watched)} watched"))
    for key in sorted(watched):
        item = repos.get(key) if isinstance(repos.get(key), dict) else {}
        name = pathlib.Path(key).name
        if not (pathlib.Path(key) / CONFIG_NAME).is_file():
            checks.append((f"Watched {name}", False, f"no {CONFIG_NAME} at {key}"))
        elif item and item.get("last_run_at"):
            checks.append((f"Watched {name}", True, f"{item.get('last_result', '?')} at {item['last_run_at']}"))
        else:
            checks.append((f"Watched {name}", True, "no check recorded yet"))
    return checks


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
    checks.extend(outcome_checks())
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
    watch = sub.add_parser("watch", help="register repositories to check when a pane resolves to nothing")
    watch.add_argument("--repo")
    watch.add_argument("--root", help="adopt every repository directly under this directory that has opted in")
    watch.add_argument("--list", action="store_true", dest="as_list")
    unwatch = sub.add_parser("unwatch", help="stop watching a repository")
    unwatch.add_argument("--repo", required=True)
    accept = sub.add_parser("accept", help="record this repository's existing failures as its baseline")
    accept.add_argument("--repo", required=True)
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
        if args.command == "watch":
            return command_watch(args.repo, args.root, args.as_list)
        if args.command == "unwatch":
            return command_unwatch(args.repo)
        if args.command == "accept":
            return command_accept(args.repo)
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
