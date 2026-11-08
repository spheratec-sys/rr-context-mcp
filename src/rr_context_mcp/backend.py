"""Thin rr/GDB adapter. Commands are passed as argv; no shell is involved."""

from __future__ import annotations

import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .mi import parse_mi_output


class DebuggerError(RuntimeError):
    """An rr/GDB operation failed or returned an error."""


class DebuggerTimeoutError(DebuggerError):
    """A bounded wait for an rr/GDB operation expired."""


class DebuggerNotRunningError(DebuggerError):
    """An interrupt was requested while the replay target was stopped."""


def _run(argv: list[str], *, cwd: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise DebuggerError(f"Required executable not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DebuggerError(f"Command timed out after {timeout}s: {argv[0]}") from exc


def _combined(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)


def _check(result: subprocess.CompletedProcess[str], what: str) -> str:
    output = _combined(result)
    if result.returncode:
        raise DebuggerError(f"{what} failed (exit {result.returncode}):\n{output}")
    return output


def traces_root() -> Path:
    return Path(os.environ.get("RR_TRACE_DIR", "~/.local/share/rr")).expanduser().resolve()


def rr_available() -> dict[str, Any]:
    """Detect the main host prerequisites without starting rr."""
    rr_path, gdb_path = shutil.which("rr"), shutil.which("gdb")
    linux = sys.platform.startswith("linux")
    paranoid: int | None = None
    try:
        paranoid = int(Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip())
    except (OSError, ValueError):
        pass
    counter_access = "unknown"
    if paranoid is not None:
        counter_access = "likely_available" if paranoid <= 1 or os.geteuid() == 0 else "likely_restricted"
    issues: list[str] = []
    if not linux:
        issues.append("rr recording requires a Linux host")
    if not rr_path:
        issues.append("rr executable was not found on PATH")
    if not gdb_path:
        issues.append("gdb executable was not found on PATH")
    if counter_access == "likely_restricted":
        issues.append(f"perf_event_paranoid={paranoid}; rr recording may require a lower value or additional privilege")
    return {"can_record": bool(linux and rr_path and counter_access != "likely_restricted"),
            "can_replay": bool(linux and rr_path and gdb_path),
            "rr_path": rr_path, "gdb_path": gdb_path, "platform": sys.platform,
            "perf_event_paranoid": paranoid, "counter_access": counter_access, "issues": issues}


def list_traces() -> list[dict[str, Any]]:
    root = traces_root()
    if not root.exists():
        return []
    traces = []
    for path in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_symlink() and path.is_dir() and (path / "version").exists():
            traces.append({"trace_id": path.name, "path": str(path), "modified": path.stat().st_mtime})
    return traces


def record(command: list[str], *, cwd: str | None = None, timeout: int = 900) -> dict[str, Any]:
    if not command:
        raise ValueError("command must contain the executable and optional arguments")
    workdir = Path(cwd or os.getcwd()).expanduser().resolve()
    if not workdir.is_dir():
        raise ValueError(f"Working directory does not exist: {workdir}")
    capability = rr_available()
    rr = capability["rr_path"]
    if not capability["can_record"]:
        raise DebuggerError("rr recording prerequisites are not met: " + "; ".join(capability["issues"]))
    root = traces_root()
    root.mkdir(parents=True, exist_ok=True)
    trace = root / f"agent-{int(time.time())}-{os.getpid()}"
    result = _run([rr, "record", "-o", str(trace), "--", *command], cwd=str(workdir), timeout=timeout)
    output = _combined(result)
    if not trace.is_dir():
        raise DebuggerError(f"rr did not produce a trace (exit {result.returncode}):\n{output[-4000:]}")
    return {"trace_id": trace.name, "trace_path": str(trace), "exit_code": result.returncode,
            "recorded": True, "cwd": str(workdir), "command": command, "output_tail": output[-12000:]}


class MIClient:
    """Minimal tokenized GDB/MI2 client with a dedicated stdout reader."""

    def __init__(self, process: subprocess.Popen[str], timeout: float = 15.0):
        self.process = process
        self.timeout = timeout
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._token = 0
        self._command_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._token_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._running = False
        self.last_stop: dict[str, Any] | None = None
        self.last_stop_record: dict[str, Any] | None = None
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._lines.put(line.rstrip("\r\n"))
        self._lines.put(None)

    def _next_token(self) -> int:
        with self._token_lock:
            self._token += 1
            return self._token

    def _send(self, command: str) -> int:
        if self.process.poll() is not None:
            raise DebuggerError("GDB session has exited")
        token = self._next_token()
        assert self.process.stdin is not None
        with self._write_lock:
            self.process.stdin.write(f"{token}{command}\n")
            self.process.stdin.flush()
        return token

    def interrupt(self) -> dict[str, Any]:
        """Request an async GDB interrupt without waiting for the active command lock."""
        with self._state_lock:
            running = self._running
        if not running:
            raise DebuggerNotRunningError("The replay target is not currently running")
        token = self._send("-exec-interrupt")
        return {"requested": True, "token": token}

    def command(self, command: str, *, wait_stop: bool = False, timeout: float | None = None) -> list[str]:
        with self._command_lock:
            if self.process.poll() is not None:
                raise DebuggerError("GDB session has exited")
            token = self._send(command)
            deadline = time.monotonic() + (timeout or self.timeout)
            lines: list[str] = []
            result_seen = False
            running_seen = False
            stopped_seen = False
            while time.monotonic() < deadline:
                try:
                    line = self._lines.get(timeout=max(0.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if line is None:
                    raise DebuggerError("GDB closed its output")
                lines.append(line)
                if line.startswith(f"{token}^error"):
                    raise DebuggerError(line)
                if line.startswith(f"{token}^"):
                    result_seen = True
                    running_seen = running_seen or line.startswith(f"{token}^running")
                if line.startswith("*running"):
                    running_seen = True
                if line.startswith("*running") or line.startswith(f"{token}^running"):
                    with self._state_lock:
                        self._running = True
                if line.startswith("*stopped") and (not wait_stop or running_seen):
                    stopped_seen = True
                if line.startswith("*stopped"):
                    with self._state_lock:
                        self._running = False
                    parsed_stop = parse_mi_output([line])
                    if parsed_stop:
                        self.last_stop_record = parsed_stop[0]
                        self.last_stop = parsed_stop[0].get("results", {})
                if result_seen and (not wait_stop or (running_seen and stopped_seen)):
                    return lines
            if wait_stop:
                with self._state_lock:
                    was_running = self._running
                if was_running:
                    try:
                        self._send("-exec-interrupt")
                    except DebuggerError:
                        pass
            raise DebuggerTimeoutError(
                f"Timed out waiting for GDB response to {command!r}; received: {' '.join(lines[-6:])}")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)


@dataclass
class ReplaySession:
    session_id: str
    trace: Path
    rr_process: subprocess.Popen[str]
    gdb_process: subprocess.Popen[str]
    mi: MIClient
    port: int
    tail: list[str] = field(default_factory=list)

    def close(self) -> None:
        self.mi.close()
        if self.rr_process.poll() is None:
            self.rr_process.terminate()
            try:
                self.rr_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.rr_process.kill()
                self.rr_process.wait(timeout=3)


class RRBackend:
    def __init__(self) -> None:
        self.sessions: dict[str, ReplaySession] = {}
        self._lock = threading.RLock()

    def record(self, command: list[str], *, cwd: str | None = None, timeout: int = 900) -> dict[str, Any]:
        return record(command, cwd=cwd, timeout=timeout)

    def list_traces(self) -> list[dict[str, Any]]:
        return list_traces()

    def start(self, trace_path: str, executable: str) -> dict[str, Any]:
        trace = Path(trace_path).expanduser().resolve()
        binary = Path(executable).expanduser().resolve()
        if not trace.is_dir():
            raise ValueError(f"Trace directory does not exist: {trace}")
        if not binary.is_file():
            raise ValueError(f"Executable does not exist: {binary}")
        capability = rr_available()
        rr, gdb = capability["rr_path"], capability["gdb_path"]
        if not capability["can_replay"]:
            raise DebuggerError("rr replay prerequisites are not met: " + "; ".join(capability["issues"]))
        # Ask rr to choose an unused port. Port 0 is supported by rr's --dbgport.
        rr_proc = subprocess.Popen([rr, "replay", "-s", "0", str(trace)], text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        port = self._wait_for_rr_port(rr_proc)
        gdb_proc = subprocess.Popen([gdb, "-q", "--nx", "--interpreter=mi2", str(binary)], text=True,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    bufsize=1)
        mi = MIClient(gdb_proc)
        session_id = f"rr-{uuid.uuid4().hex[:12]}"
        session = ReplaySession(session_id, trace, rr_proc, gdb_proc, mi, port)
        try:
            mi.command("-gdb-set pagination off")
            mi.command(f"-target-select remote 127.0.0.1:{port}", timeout=60)
        except Exception:
            session.close()
            raise
        with self._lock:
            self.sessions[session_id] = session
        return {"session_id": session_id, "trace_path": str(trace), "executable": str(binary), "port": port,
                "state": self._location(session)}

    @staticmethod
    def _wait_for_rr_port(proc: subprocess.Popen[str], timeout: float = 30) -> int:
        assert proc.stdout is not None
        # rr reports the selected ephemeral port in its combined output.
        deadline = time.monotonic() + timeout
        collected = ""
        fd = proc.stdout.fileno()
        import selectors
        selector = selectors.DefaultSelector()
        selector.register(fd, selectors.EVENT_READ)
        try:
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise DebuggerError(f"rr replay server exited: {collected[-4000:]}")
                if selector.select(0.2):
                    chunk = os.read(fd, 4096).decode(errors="replace")
                    collected += chunk
                    match = re.search(r"(?:port|Port)\s*[:=]?\s*(\d{4,5})", collected)
                    if match:
                        return int(match.group(1))
        finally:
            selector.close()
        proc.terminate()
        raise DebuggerError(f"Timed out waiting for rr replay server port. Output: {collected[-4000:]}")

    def get(self, session_id: str) -> ReplaySession:
        try:
            return self.sessions[session_id]
        except KeyError as exc:
            raise ValueError(f"Unknown replay session: {session_id}") from exc

    @staticmethod
    def _location(session: ReplaySession) -> dict[str, Any]:
        records = parse_mi_output(session.mi.command("-stack-info-frame"))
        result = next((r for r in records if r["record"] == "result"), {})
        return {"frame": _normalize_frame(result.get("results", {}).get("frame")),
                "stop_reason": _normalize_stop(getattr(session.mi, "last_stop", None) or {})}

    def inspect(self, session_id: str) -> dict[str, Any]:
        s = self.get(session_id)
        bt = parse_mi_output(s.mi.command("-stack-list-frames"))
        locals_ = parse_mi_output(s.mi.command("-stack-list-locals 1"))
        threads = parse_mi_output(s.mi.command("-thread-info"))
        position = self.when(session_id)
        stack_results = _result_data(bt)
        local_results = _result_data(locals_)
        thread_results = _result_data(threads)
        stop = getattr(s.mi, "last_stop", None) or {}
        stack = stack_results.get("stack", [])
        frames = [_normalize_frame(entry.get("frame", entry) if isinstance(entry, dict) else entry)
                  for entry in stack]
        warnings = _parse_warnings(bt + locals_ + threads)
        warnings.extend(position.get("parse_warnings", []))
        last_stop_record = getattr(s.mi, "last_stop_record", None)
        if last_stop_record and last_stop_record.get("parse_error"):
            warnings.append({"parse_error": last_stop_record["parse_error"],
                             "raw": last_stop_record.get("raw")})
        stop_reason = _normalize_stop(stop)
        frame = frames[0] if frames else stop_reason["frame"]
        return {"ok": True, "session_id": session_id, "frame": frame,
                "backtrace": frames,
                "locals": _as_list(local_results.get("locals")),
                "threads": _as_list(thread_results.get("threads")),
                "current_thread_id": thread_results.get("current-thread-id"),
                "stop_reason": stop_reason,
                "position": {"event": position.get("event"), "ticks": position.get("ticks")},
                "parse_warnings": warnings}

    def when(self, session_id: str) -> dict[str, Any]:
        """Ask rr for its global event and current-thread tick coordinates."""
        mi = self.get(session_id).mi
        event = parse_mi_output(mi.command('-interpreter-exec console "when"'))
        ticks = parse_mi_output(mi.command('-interpreter-exec console "when-ticks"'))
        event_text, ticks_text = _stream_text(event), _stream_text(ticks)
        return {"event": _position_number(event_text, _result_data(event)),
                "ticks": _position_number(ticks_text, _result_data(ticks)),
                "event_text": event_text, "ticks_text": ticks_text,
                "parse_warnings": _parse_warnings(event + ticks)}

    def move(self, session_id: str, action: str, *, count: int = 1) -> dict[str, Any]:
        commands = {"continue": "-exec-continue", "reverse_continue": "-exec-reverse-continue",
                    "step": "-exec-step", "reverse_step": "-exec-reverse-step",
                    "next": "-exec-next", "reverse_next": "-exec-next --reverse"}
        if action not in commands:
            raise ValueError(f"Unsupported execution action: {action}")
        if not 1 <= count <= 100:
            raise ValueError("count must be between 1 and 100")
        s = self.get(session_id)
        output: list[dict[str, Any]] = []
        for _ in range(count):
            timeout_name = "RR_MCP_TIMEOUT_REVERSE" if action.startswith("reverse") else "RR_MCP_TIMEOUT_FORWARD"
            default_timeout = "300" if action.startswith("reverse") else "120"
            timeout = float(os.environ.get(timeout_name, default_timeout))
            output.extend(parse_mi_output(s.mi.command(commands[action], wait_stop=True, timeout=timeout)))
        evidence = self.inspect(session_id)
        evidence["parse_warnings"] = evidence["parse_warnings"] + _parse_warnings(output[-40:])
        return evidence

    def interrupt(self, session_id: str) -> dict[str, Any]:
        result = self.get(session_id).mi.interrupt()
        return {"ok": True, "session_id": session_id,
                "status": "interrupt_requested", "requested": result["requested"]}

    def gdb(self, session_id: str, command: str) -> dict[str, Any]:
        # The MVP exposes only an allow-listed, single expression via GDB's MI
        # evaluator. Arbitrary console commands would permit shell escapes.
        if not command.strip() or len(command) > 500:
            raise ValueError("expression must be 1-500 characters")
        lines = parse_mi_output(self.get(session_id).mi.command(f"-data-evaluate-expression {self._mi_quote(command)}"))
        return {"session_id": session_id, "expression": command,
                "result": _result_data(lines)}

    @staticmethod
    def _mi_quote(value: str) -> str:
        import json
        return json.dumps(value)

    def breakpoint(self, session_id: str, location: str) -> dict[str, Any]:
        if not location.strip() or len(location) > 300:
            raise ValueError("location must be 1-300 characters")
        lines = parse_mi_output(self.get(session_id).mi.command(f"-break-insert {self._mi_quote(location)}"))
        return {"session_id": session_id, "breakpoint": _result_data(lines)}

    def watchpoint(self, session_id: str, expression: str, access: str = "write") -> dict[str, Any]:
        """Stop when a watched expression changes; rr/GDB hardware support applies."""
        if access not in {"write", "read", "access"}:
            raise ValueError("access must be write, read, or access")
        if not expression.strip() or len(expression) > 300:
            raise ValueError("expression must be 1-300 characters")
        if any(ch in expression for ch in "\n\r;`$"):
            raise ValueError("expression contains unsupported command characters")
        options = {"write": "", "read": " -r", "access": " -a"}[access]
        lines = parse_mi_output(self.get(session_id).mi.command(f"-break-watch{options} {expression.strip()}"))
        return {"session_id": session_id, "expression": expression, "access": access,
                "watchpoint": _result_data(lines)}

    def close(self, session_id: str) -> dict[str, str]:
        with self._lock:
            s = self.sessions.pop(session_id, None)
        if not s:
            raise ValueError(f"Unknown replay session: {session_id}")
        s.close()
        return {"session_id": session_id, "state": "closed"}


def _result_data(records: list[dict[str, Any]]) -> dict[str, Any]:
    return next((r.get("results", {}) for r in records if r["record"] == "result"), {})


def _stream_text(records: list[dict[str, Any]]) -> str:
    return "".join(r.get("text", "") for r in records if r["record"] == "stream")


def _position_number(text: str, results: dict[str, Any]) -> int | None:
    """Extract a numeric rr coordinate while keeping display text separately."""
    for key in ("event", "ticks", "value"):
        value = results.get(key)
        if isinstance(value, (int, float)):
            return int(value)
        if value is not None:
            match = re.search(r"\d+", str(value))
            if match:
                return int(match.group())
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def _parse_warnings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"parse_error": str(record["parse_error"]), "raw": record.get("raw")}
            for record in records if record.get("parse_error")]


def _normalize_frame(frame: Any) -> dict[str, Any] | None:
    """Map GDB frame fields to a fixed, agent-facing shape."""
    if not isinstance(frame, dict):
        return None
    return {
        "level": frame.get("level"),
        "address": frame.get("addr", frame.get("address")),
        "function": frame.get("func", frame.get("function")),
        "file": frame.get("file"),
        "fullname": frame.get("fullname"),
        "line": frame.get("line"),
    }


def _normalize_stop(stop: Any) -> dict[str, Any]:
    stop = stop if isinstance(stop, dict) else {}
    known = {"reason", "signal-name", "signal-meaning", "thread-id", "stopped-threads", "frame",
             "raw", "parse_error"}
    frame = _normalize_frame(stop.get("frame"))
    return {
        "reason": stop.get("reason"),
        "signal_name": stop.get("signal-name"),
        "signal_meaning": stop.get("signal-meaning"),
        "thread_id": stop.get("thread-id"),
        "stopped_threads": stop.get("stopped-threads"),
        "frame": frame,
        "details": {key: value for key, value in stop.items() if key not in known},
    }


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
