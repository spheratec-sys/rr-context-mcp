from rr_context_mcp.backend import DebuggerError, DebuggerNotRunningError, DebuggerTimeoutError
from rr_context_mcp.server import _call


def raise_runtime():
    raise DebuggerError("rr is missing")


def raise_invalid():
    raise ValueError("bad input")


def raise_timeout():
    raise DebuggerTimeoutError("continue timed out")


def raise_not_running():
    raise DebuggerNotRunningError("target is stopped")


def raise_unexpected():
    raise RuntimeError("unexpected failure")


def test_expected_runtime_failures_are_structured():
    result = _call(raise_runtime)
    assert result == {"ok": False, "error": {
        "code": "runtime_unavailable", "message": "rr is missing"}}


def test_invalid_arguments_are_structured():
    result = _call(raise_invalid)
    assert result == {"ok": False, "error": {
        "code": "invalid_argument", "message": "bad input"}}


def test_timeouts_are_structured_for_mcp_clients():
    result = _call(raise_timeout)
    assert result == {"ok": False, "error": {
        "code": "timeout", "message": "continue timed out"}}


def test_interrupt_not_running_is_structured():
    result = _call(raise_not_running)
    assert result == {"ok": False, "error": {
        "code": "not_running", "message": "target is stopped"}}


def test_unexpected_errors_still_use_standard_shape():
    result = _call(raise_unexpected)
    assert result == {"ok": False, "error": {
        "code": "internal_error", "message": "unexpected failure"}}
