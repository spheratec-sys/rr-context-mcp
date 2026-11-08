from io import StringIO

import pytest

from rr_context_mcp.backend import DebuggerError, MIClient


class FakeProcess:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.stdin = StringIO()

    def poll(self):
        return None


def test_execution_wait_requires_a_fresh_stop_after_running():
    process = FakeProcess([
        '*stopped,reason="breakpoint-hit",bkptno="1"',  # stale stop from prior request
        '*running,thread-id="all"',
        '1^running',
        '*stopped,reason="end-stepping-range"',
    ])
    result = MIClient(process).command("-exec-step", wait_stop=True, timeout=1)
    assert result[-1] == '*stopped,reason="end-stepping-range"'
    assert process.stdin.getvalue() == "1-exec-step\n"


def test_stale_stop_does_not_complete_execution_request():
    process = FakeProcess([
        '*stopped,reason="breakpoint-hit"',
        '1^running',
    ])
    with pytest.raises(DebuggerError, match="closed its output"):
        MIClient(process).command("-exec-continue", wait_stop=True, timeout=1)


def test_interrupt_sends_async_mi_command_without_command_lock():
    process = FakeProcess([])
    client = MIClient(process)
    client._running = True
    result = client.interrupt()
    assert result["requested"] is True
    assert process.stdin.getvalue() == "1-exec-interrupt\n"
