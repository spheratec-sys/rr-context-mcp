"""MCP tools for capturing and investigating rr executions."""

from __future__ import annotations

import argparse
import os

from mcp.server import MCPServer

from . import __version__
from .backend import (DebuggerError, DebuggerNotRunningError, DebuggerTimeoutError,
                      RRBackend, rr_available)

mcp = MCPServer("rr-context-mcp", version=__version__)
backend = RRBackend()


class DryRunBackend:
    """Deterministic demo backend for exercising MCP tools without rr/GDB."""
    is_dry_run = True

    def __init__(self):
        self.sessions: dict[str, dict] = {}

    def record(self, command, *, cwd=None, timeout=900):
        return {"trace_id": "demo-trace", "trace_path": "/tmp/rr-context-mcp-demo-trace",
                "recorded": True, "exit_code": 139, "cwd": cwd or os.getcwd(),
                "command": command, "output_tail": "Segmentation fault (demo)"}

    def list_traces(self):
        return [{"trace_id": "demo-trace", "path": "/tmp/rr-context-mcp-demo-trace", "demo": True}]

    def start(self, trace_path, executable):
        session_id = "demo-session"
        frame = {"level": "0", "address": "0x4011a2", "function": "parse_input",
                 "file": "src/parser.c", "fullname": "/demo/src/parser.c", "line": "84"}
        backtrace = [frame,
                     {"level": "1", "address": "0x401300", "function": "process_packet",
                      "file": "src/worker.c", "fullname": "/demo/src/worker.c", "line": "211"},
                     {"level": "2", "address": "0x401050", "function": "main",
                      "file": "src/main.c", "fullname": "/demo/src/main.c", "line": "31"}]
        stop_reason = {"reason": "signal-received", "signal_name": "SIGSEGV",
                       "signal_meaning": "Segmentation fault", "thread_id": "3",
                       "stopped_threads": "all", "frame": frame,
                       "details": {"address": "0x0"}}
        self.sessions[session_id] = {
            "trace_path": trace_path, "executable": executable,
            "backtrace": backtrace,
            "locals": [{"name": "cursor", "value": "0x0"},
                       {"name": "packet", "value": "0x7fffffffdc20"},
                       {"name": "length", "value": "4096"}],
            "threads": [{"id": "1", "state": "stopped", "name": "main"},
                        {"id": "2", "state": "stopped", "name": "io-worker"},
                        {"id": "3", "state": "stopped", "name": "parser"}],
            "current_thread_id": "3", "position": {"event": 481, "ticks": 90214},
            "stop_reason": stop_reason, "watchpoint": None, "parse_warnings": []}
        return {"session_id": session_id, "trace_path": trace_path, "executable": executable,
                "state": {"frame": frame, "stop_reason": stop_reason}}

    def inspect(self, session_id):
        session = self._get(session_id)
        backtrace = session["backtrace"]
        frame = backtrace[0] if backtrace else session["stop_reason"]["frame"]
        return {"ok": True, "session_id": session_id, "frame": frame,
                "backtrace": list(backtrace),
                "locals": list(session["locals"]),
                "threads": list(session["threads"]),
                "current_thread_id": session["current_thread_id"],
                "position": dict(session["position"]),
                "stop_reason": dict(session["stop_reason"]),
                "parse_warnings": list(session["parse_warnings"])}

    def when(self, session_id):
        self._get(session_id)
        return {"event": 481, "ticks": 90214,
                "event_text": "demo event 481", "ticks_text": "demo ticks 90214",
                "parse_warnings": []}

    def move(self, session_id, action, *, count=1):
        session = self._get(session_id)
        if action == "reverse_continue" and session["watchpoint"]:
            frame = {"level": "0", "address": "0x40109c", "function": "advance_cursor",
                     "file": "src/parser.c", "fullname": "/demo/src/parser.c", "line": "62"}
            session["backtrace"] = [frame,
                                    {"level": "1", "address": "0x401300", "function": "process_packet",
                                     "file": "src/worker.c", "fullname": "/demo/src/worker.c", "line": "211"},
                                    {"level": "2", "address": "0x401050", "function": "main",
                                     "file": "src/main.c", "fullname": "/demo/src/main.c", "line": "31"}]
            session["position"] = {"event": 350, "ticks": 67102}
            session["stop_reason"] = {
                "reason": "watchpoint-trigger", "signal_name": None, "signal_meaning": None,
                "thread_id": "3", "stopped_threads": "all", "frame": frame,
                "details": {"wpnum": "2", "expression": session["watchpoint"],
                            "oldvalue": "4096", "value": "0"}}
            session["locals"] = [{"name": "cursor", "value": "0"},
                                 {"name": "length", "value": "4096"},
                                 {"name": "packet", "value": "0x7fffffffdc20"}]
        else:
            frame = session["backtrace"][0]
            session["stop_reason"] = {"reason": "end-stepping-range", "signal_name": None,
                                      "signal_meaning": None, "thread_id": session["current_thread_id"],
                                      "stopped_threads": "all", "frame": frame, "details": {}}
        return self.inspect(session_id)

    def interrupt(self, session_id):
        self._get(session_id)
        return {"ok": True, "session_id": session_id, "status": "interrupt_requested", "requested": True}

    def breakpoint(self, session_id, location):
        self._get(session_id)
        return {"session_id": session_id, "breakpoint": {"number": "1", "original-location": location}}

    def watchpoint(self, session_id, expression, access="write"):
        self._get(session_id)["watchpoint"] = expression
        return {"session_id": session_id, "expression": expression, "access": access,
                "watchpoint": {"number": "2", "exp": expression}}

    def gdb(self, session_id, expression):
        self._get(session_id)
        return {"session_id": session_id, "expression": expression, "result": {"value": "0x0"}}

    def close(self, session_id):
        self._get(session_id)
        self.sessions.pop(session_id)
        return {"session_id": session_id, "state": "closed"}

    def _get(self, session_id):
        if session_id not in self.sessions:
            raise ValueError(f"Unknown replay session: {session_id}")
        return self.sessions[session_id]


def _call(function, *args, **kwargs):
    """Keep expected runtime and input failures structured for MCP clients."""
    try:
        return function(*args, **kwargs)
    except Exception as exc:
        code = ("invalid_argument" if isinstance(exc, ValueError) else
                "timeout" if isinstance(exc, DebuggerTimeoutError) else
                "not_running" if isinstance(exc, DebuggerNotRunningError) else
                "runtime_unavailable" if isinstance(exc, (DebuggerError, OSError)) else "internal_error")
        return {"ok": False, "error": {"code": code, "message": str(exc)}}


@mcp.tool()
def rr_record(command: list[str], cwd: str = "", timeout_seconds: int = 900) -> dict:
    """Record a failing or suspicious program run with rr.

    Pass the executable and each argument as separate array items. The command
    is executed directly, without a shell. Use the same build and inputs that
    reproduce the bug. Returns a trace_id and trace_path for replay_start.
    """
    if not 1 <= timeout_seconds <= 3600:
        return {"ok": False, "error": {"code": "invalid_argument",
                "message": "timeout_seconds must be between 1 and 3600"}}
    return _call(backend.record, command, cwd=cwd or None, timeout=timeout_seconds)


@mcp.tool()
def rr_environment() -> dict:
    """Check whether this host appears ready to record and replay with rr."""
    if getattr(backend, "is_dry_run", False):
        return {"can_record": True, "can_replay": True, "mode": "dry_run",
                "issues": ["Demo responses are active; no rr recording or replay occurs."]}
    return _call(rr_available)


@mcp.tool()
def rr_list_traces() -> list[dict]:
    """List local rr recordings, newest first. Use trace_path with rr_replay_start."""
    return _call(backend.list_traces)


@mcp.tool()
def rr_replay_start(trace_path: str, executable: str) -> dict:
    """Start an interactive replay session for a trace and its debug-symbol binary.

    Provide the exact executable used by the recording, preferably an unstripped
    build with debug symbols. The response includes session_id and the initial
    stopped location. Use rr_inspect to collect context before moving execution.
    """
    return _call(backend.start, trace_path, executable)


@mcp.tool()
def rr_inspect(session_id: str) -> dict:
    """Return the current stack, selected-frame locals, thread summary, and stop reason.

    Call after replay_start and after each execution movement. This compact
    evidence bundle is the best starting point for root-cause analysis.
    """
    return _call(backend.inspect, session_id)


@mcp.tool()
def rr_root_cause_hint(session_id: str, symptom: str = "auto") -> dict:
    """Return an investigation checklist based on stop reason and observed symptom.

    symptom may be auto, segfault, assertion, wrong_value, or hang. Use wrong_value
    after a breakpoint/watchpoint exposes the incorrect state; use hang when
    replay is stopped at a chosen progress marker for a suspected stuck path.
    """
    if symptom not in {"auto", "segfault", "assertion", "wrong_value", "hang"}:
        return {"ok": False, "error": {"code": "invalid_argument",
                "message": "symptom must be auto, segfault, assertion, wrong_value, or hang"}}
    evidence = _call(backend.inspect, session_id)
    if not evidence.get("ok", True):
        return evidence
    stop = evidence.get("stop_reason") or {}
    reason = str(stop.get("reason", "unknown"))
    signal = str(stop.get("signal_name", ""))
    if symptom == "segfault" or (symptom == "auto" and reason == "signal-received" and signal in {"SIGSEGV", "SIGBUS", "SIGILL"}):
        category = "memory_or_instruction_fault"
        checklist = ["Inspect the faulting frame and its arguments/locals.",
                     "Identify the invalid address or corrupted value from the stop context.",
                     "Set a write watchpoint on the suspect pointer/value if it was valid earlier.",
                     "Use reverse_step or reverse_continue to find the earlier state change."]
    elif symptom == "assertion" or (symptom == "auto" and reason == "signal-received" and signal == "SIGABRT"):
        category = "assertion_or_abort"
        checklist = ["Inspect the assertion/abort frame and its caller.",
                     "Read the condition and relevant values in the frame locals.",
                     "Move backward to the point where the asserted invariant first became false."]
    elif symptom == "wrong_value" or "watchpoint" in reason:
        category = "watched_state_changed"
        checklist = ["Record the old and new values and the writing frame.",
                     "Inspect the current thread and nearby caller frames.",
                     "Move backward to confirm the earliest write that violates the expected invariant."]
    elif symptom == "hang":
        category = "suspected_hang"
        checklist = ["Compare the current rr event/tick position with the last known progress marker.",
                     "Inspect the selected thread and stack for a blocking call or repeated loop.",
                     "Set a breakpoint at the expected exit/progress point and compare thread states.",
                     "Use rr_interrupt to stop a running replay, or rely on rr_move's bounded timeout."]
    elif "breakpoint" in reason:
        category = "breakpoint_hit"
        checklist = ["Compare arguments and locals with the expected invariant.",
                     "Continue to the failure or reverse to the previous state transition."]
    elif "exited" in reason:
        category = "process_exit"
        checklist = ["Check whether this is the expected process and exit code.",
                     "Inspect the final frames and move backward to the last relevant operation."]
    else:
        category = "unknown_stop"
        checklist = ["Inspect the stop reason, top stack frames, and selected thread.",
                     "If the failure has not occurred yet, continue to it or set a breakpoint.",
                     "Once the bad state is visible, reverse to identify its cause."]
    return {"session_id": session_id, "category": category, "symptom": symptom, "signal": signal or None,
            "stop_reason": reason, "position": evidence.get("position"), "checklist": checklist}


@mcp.tool()
def rr_when(session_id: str) -> dict:
    """Return rr's current event number and per-thread tick position.

    Use these coordinates to compare observations and to describe where in the
    recorded execution a failure or state transition occurred.
    """
    return _call(backend.when, session_id)


@mcp.tool()
def rr_move(session_id: str, action: str, count: int = 1) -> dict:
    """Move deterministically through the recording.

    action is one of continue, reverse_continue, step, reverse_step, next,
    reverse_next. For memory corruption, set a breakpoint/watchpoint near the
    observed bad state, then reverse_continue to find the earlier cause.
    """
    return _call(backend.move, session_id, action, count=count)


@mcp.tool()
def rr_interrupt(session_id: str) -> dict:
    """Interrupt a currently running continue or reverse-continue operation."""
    return _call(backend.interrupt, session_id)


@mcp.tool()
def rr_breakpoint(session_id: str, location: str) -> dict:
    """Set a GDB breakpoint by function, source.cpp:line, or address expression."""
    return _call(backend.breakpoint, session_id, location)


@mcp.tool()
def rr_watchpoint(session_id: str, expression: str, access: str = "write") -> dict:
    """Watch a value and stop when it is written, read, or accessed.

    A write watchpoint is useful after observing a corrupted variable: set it
    while the value is still correct, then continue or reverse through the trace
    to identify the instruction that changes it. Hardware watchpoint limits vary.
    """
    return _call(backend.watchpoint, session_id, expression, access)


@mcp.tool()
def rr_evaluate(session_id: str, expression: str) -> dict:
    """Evaluate one C/C++ expression in the current frame; does not run a GDB console command."""
    return _call(backend.gdb, session_id, expression)


@mcp.tool()
def rr_session_close(session_id: str) -> dict:
    """Close the replay and GDB processes for a session."""
    return _call(backend.close, session_id)


@mcp.resource("rr://guide")
def debugging_guide() -> str:
    """A root-cause workflow for using rr-context-mcp tools."""
    return """# rr root-cause workflow

1. Reproduce the failing command under rr_record. The exact command, arguments,
   environment, working directory, inputs, and build should match the failure.
2. Start rr_replay_start with the returned trace_path and the matching executable
   that has debug symbols. rr replay is deterministic, so the failure can be
   revisited without reproducing it live.
3. Call rr_inspect at the initial stop or after continuing to a crash/breakpoint.
   Record the stop reason, top frames, locals, and relevant thread.
4. Choose a path based on the symptom:
   - SIGSEGV/SIGBUS/SIGILL: inspect the faulting instruction, address, arguments,
     and caller; identify which pointer or index is invalid.
   - SIGABRT/assertion: inspect the assertion frame and condition, then move
     backward until the asserted invariant first becomes false.
   - Wrong value: stop where the bad value is visible, set a write watchpoint,
     and reverse to the instruction that first changed it.
   - Suspected hang: compare rr event/tick positions and thread stacks at known
     progress points; use breakpoints to bracket where progress stops.
5. Use rr_root_cause_hint for a stop-specific checklist. Use rr_when to record
   event/tick coordinates and rr_move to step or continue in either direction.
6. Explain the causal chain with observed values, frames, thread, and position,
   then close the session.

Keep the trace path and session_id associated with the same investigation. rr
records local process execution; it does not capture arbitrary external services,
network peers, or machine-wide state. Re-run recording when inputs or code change.
"""


def main() -> None:
    global backend
    parser = argparse.ArgumentParser(description="MCP server for rr record/replay debugging")
    parser.add_argument("--dry-run", action="store_true", help="serve deterministic demo results without rr or gdb")
    args = parser.parse_args()
    if args.dry_run:
        backend = DryRunBackend()
    mcp.run()


if __name__ == "__main__":
    main()
