from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rr_context_mcp import backend
from rr_context_mcp.backend import DebuggerError, RRBackend


def test_record_uses_argv_and_preserves_exit_code(monkeypatch, tmp_path):
    trace_root = tmp_path / "traces"
    monkeypatch.setenv("RR_TRACE_DIR", str(trace_root))
    monkeypatch.setattr(backend.shutil, "which", lambda exe: "/usr/bin/rr")
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        trace_path = Path(argv[3])
        trace_path.mkdir(parents=True)
        (trace_path / "version").write_text("0.1")
        return subprocess.CompletedProcess(argv, 7, "program output", "")

    monkeypatch.setattr(backend, "_run", fake_run)
    result = backend.record(["./test-binary", "arg with spaces"], cwd=str(tmp_path))
    assert seen["argv"][:3] == ["/usr/bin/rr", "record", "-o"]
    assert seen["argv"][-3:] == ["--", "./test-binary", "arg with spaces"]
    assert result["exit_code"] == 7
    assert result["recorded"] is True
    assert result["output_tail"] == "program output"


def test_record_rejects_empty_command(tmp_path):
    with pytest.raises(ValueError, match="command"):
        backend.record([], cwd=str(tmp_path))


def test_record_reports_missing_rr(monkeypatch, tmp_path):
    monkeypatch.setattr(backend.shutil, "which", lambda _: None)
    with pytest.raises(DebuggerError, match="rr executable was not found"):
        backend.record(["true"], cwd=str(tmp_path))


def test_rr_available_reports_missing_runtime(monkeypatch):
    monkeypatch.setattr(backend.shutil, "which", lambda _: None)
    result = backend.rr_available()
    assert result["can_record"] is False
    assert result["can_replay"] is False
    assert any("rr executable" in issue for issue in result["issues"])
    assert any("gdb executable" in issue for issue in result["issues"])


class FakeMI:
    def __init__(self):
        self.calls = []
        self.last_stop = {"reason": "breakpoint-hit"}

    def command(self, command, **kwargs):
        self.calls.append(command)
        if command == "-stack-info-frame":
            return ["^done,frame={level=0,func=main}"]
        if command == "-stack-list-frames":
            return ["^done,stack=[frame={level=0,func=main}]", '*stopped,reason="breakpoint-hit"']
        if command == "-stack-list-locals 1":
            return ["^done,locals=[{name=x,value=1}]"]
        if command == "-thread-info":
            return ["^done,threads=[]"]
        if command.startswith("-data-evaluate-expression"):
            return ['^done,value="42"']
        if command.startswith("-exec-step"):
            return ['*running,thread-id="all"', "1^running", '*stopped,reason="end-stepping-range"']
        return ["^done"]


def make_backend():
    obj = RRBackend()
    obj.sessions["s1"] = type("S", (), {"mi": FakeMI(), "trace": None})()
    return obj


def test_inspect_returns_compact_evidence_bundle():
    obj = make_backend()
    result = obj.inspect("s1")
    assert set(result) == {"ok", "session_id", "frame", "backtrace", "locals", "threads",
                           "current_thread_id", "stop_reason", "position", "parse_warnings"}
    assert result["ok"] is True
    assert result["session_id"] == "s1"
    assert result["frame"]["function"] == "main"
    assert set(result["frame"]) == {"level", "address", "function", "file", "fullname", "line"}
    assert result["frame"] == result["backtrace"][0]
    assert result["position"] == {"event": None, "ticks": None}
    assert set(result["stop_reason"]) == {"reason", "signal_name", "signal_meaning", "thread_id",
                                          "stopped_threads", "frame", "details"}
    assert result["stop_reason"]["reason"] == "breakpoint-hit"
    assert result["parse_warnings"] == []


def test_inspect_uses_empty_lists_instead_of_null_for_missing_collections():
    obj = make_backend()
    fake = obj.get("s1").mi
    original = fake.command

    def empty_collections(command, **kwargs):
        if command in {"-stack-list-frames", "-stack-list-locals 1", "-thread-info"}:
            return ["^done"]
        return original(command, **kwargs)

    fake.command = empty_collections
    result = obj.inspect("s1")
    assert result["backtrace"] == []
    assert result["locals"] == []
    assert result["threads"] == []
    assert result["frame"] is None


def test_initial_live_location_uses_normalized_frame_and_stop_shapes():
    obj = make_backend()
    state = RRBackend._location(obj.get("s1"))
    assert set(state["frame"]) == {"level", "address", "function", "file", "fullname", "line"}
    assert set(state["stop_reason"]) == {"reason", "signal_name", "signal_meaning", "thread_id",
                                          "stopped_threads", "frame", "details"}


def test_move_validates_action_and_count():
    obj = make_backend()
    with pytest.raises(ValueError, match="Unsupported"):
        obj.move("s1", "jump")
    with pytest.raises(ValueError, match="count"):
        obj.move("s1", "step", count=0)


def test_interrupt_success_is_structured():
    obj = make_backend()
    obj.get("s1").mi.interrupt = lambda: {"requested": True, "token": 7}
    assert obj.interrupt("s1") == {
        "ok": True, "session_id": "s1", "status": "interrupt_requested", "requested": True}


def test_move_returns_parsed_execution_records():
    obj = make_backend()
    result = obj.move("s1", "step")
    assert set(result) == {"ok", "session_id", "frame", "backtrace", "locals", "threads",
                           "current_thread_id", "stop_reason", "position", "parse_warnings"}
    assert result["ok"] is True
    assert result["frame"]["function"] == "main"
    assert isinstance(result["locals"], list)
    assert isinstance(result["threads"], list)
    assert set(result["position"]) == {"event", "ticks"}
    assert result["stop_reason"]["reason"] == "breakpoint-hit"


def test_inspect_keeps_partial_structured_data_for_malformed_mi():
    obj = make_backend()
    fake = obj.get("s1").mi
    original = fake.command
    def malformed(command, **kwargs):
        if command == "-stack-list-frames":
            return ['^done,stack=[frame={level="0",func="partial",line="8"},frame={level="1",func="cut"']
        return original(command, **kwargs)
    fake.command = malformed
    result = obj.inspect("s1")
    assert result["backtrace"][0]["function"] == "partial"
    assert result["parse_warnings"]
    assert result["parse_warnings"][0]["raw"].startswith("^done,stack=")
    assert set(result["parse_warnings"][0]) == {"parse_error", "raw"}
    assert isinstance(result["parse_warnings"][0]["parse_error"], str)
    assert "raw" not in result["backtrace"][0]


def test_evaluate_returns_parsed_value():
    result = make_backend().gdb("s1", "answer")
    assert result["result"]["value"] == "42"


def test_mi_quote_escapes_expression():
    assert RRBackend._mi_quote('a"b\\c') == '"a\\\"b\\\\c"'


def test_watchpoint_maps_access_modes():
    obj = make_backend()
    result = obj.watchpoint("s1", "state.value", "read")
    assert result["access"] == "read"
    assert obj.get("s1").mi.calls[-1] == "-break-watch -r state.value"


def test_watchpoint_rejects_command_separators():
    obj = make_backend()
    with pytest.raises(ValueError, match="unsupported"):
        obj.watchpoint("s1", "value; quit")
