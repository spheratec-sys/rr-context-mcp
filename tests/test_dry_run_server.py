import json
import os
import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from rr_context_mcp.server import DryRunBackend


EVIDENCE_KEYS = {"ok", "session_id", "frame", "backtrace", "locals", "threads",
                 "current_thread_id", "stop_reason", "position", "parse_warnings"}


def test_dry_run_inspect_move_and_interrupt_contract():
    backend = DryRunBackend()
    started = backend.start("/tmp/trace", "/tmp/app")
    inspected = backend.inspect("demo-session")
    moved = backend.move("demo-session", "step")
    assert set(started["state"]["frame"]) == {"level", "address", "function", "file", "fullname", "line"}
    assert set(started["state"]["stop_reason"]) == {"reason", "signal_name", "signal_meaning",
                                                     "thread_id", "stopped_threads", "frame", "details"}
    assert set(inspected) == EVIDENCE_KEYS
    assert set(moved) == EVIDENCE_KEYS
    assert inspected["ok"] is moved["ok"] is True
    assert set(inspected["position"]) == {"event", "ticks"}
    assert inspected["frame"] == inspected["backtrace"][0]
    for evidence in (inspected, moved):
        assert isinstance(evidence["backtrace"], list)
        assert isinstance(evidence["locals"], list)
        assert isinstance(evidence["threads"], list)
        assert isinstance(evidence["parse_warnings"], list)
        assert set(evidence["frame"]) == {"level", "address", "function", "file", "fullname", "line"}
        assert set(evidence["stop_reason"]) == {"reason", "signal_name", "signal_meaning",
                                                "thread_id", "stopped_threads", "frame", "details"}
    assert backend.interrupt("demo-session") == {
        "ok": True, "session_id": "demo-session", "status": "interrupt_requested", "requested": True}


def test_dry_run_mcp_server_handles_tool_workflow():
    import asyncio

    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "rr_context_mcp.server", "--dry-run"], env=env, cwd=root)

    async def workflow():
        async with Client(params, read_timeout_seconds=10) as client:
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert "rr_root_cause_hint" in names and "rr_watchpoint" in names
            assert "rr_interrupt" in names
            await client.call_tool("rr_record", {"command": ["demo-app"]})
            await client.call_tool("rr_replay_start", {"trace_path": "/tmp/demo", "executable": "/tmp/demo-app"})
            inspect = await client.call_tool("rr_inspect", {"session_id": "demo-session"})
            inspect_data = json.loads(inspect.content[0].text)
            assert set(inspect_data) == EVIDENCE_KEYS
            hint = await client.call_tool("rr_root_cause_hint", {"session_id": "demo-session"})
            assert "SIGSEGV" in str(inspect)
            assert '"frame"' in str(inspect)
            assert "memory_or_instruction_fault" in str(hint)
            watch = await client.call_tool("rr_watchpoint", {
                "session_id": "demo-session", "expression": "cursor", "access": "write"})
            assert "cursor" in str(watch)
            moved = await client.call_tool("rr_move", {
                "session_id": "demo-session", "action": "reverse_continue"})
            moved_data = json.loads(moved.content[0].text)
            assert set(moved_data) == EVIDENCE_KEYS
            assert moved_data["stop_reason"]["reason"] == "watchpoint-trigger"
            assert moved_data["stop_reason"]["details"]["oldvalue"] == "4096"
            hint_after_reverse = await client.call_tool("rr_root_cause_hint", {
                "session_id": "demo-session"})
            assert "watched_state_changed" in str(hint_after_reverse)

    asyncio.run(workflow())
