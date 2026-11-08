from pathlib import Path

from rr_context_mcp.mi import parse_mi_line, parse_mi_output


def test_parses_nested_result_records():
    parsed = parse_mi_line('12^done,stack=[frame={level="0",func="main",line="7"}]')
    assert parsed == {
        "record": "result",
        "token": 12,
        "class": "done",
        "results": {"stack": [{"frame": {"level": "0", "func": "main", "line": "7"}}]},
    }


def test_parses_mixed_local_and_thread_lists():
    records = parse_mi_output([
        '^done,locals=[{name="answer",value="42"}]',
        '^done,current-thread-id="2",threads=[{id="2",state="stopped"}]',
    ])
    assert records[0]["results"]["locals"] == [{"name": "answer", "value": "42"}]
    assert records[1]["results"]["current-thread-id"] == "2"
    assert records[1]["results"]["threads"] == [{"id": "2", "state": "stopped"}]


def test_parses_console_stream_and_async_stop():
    stream = parse_mi_line('~"Current event: 123\\n"')
    stopped = parse_mi_line('*stopped,reason="signal-received",signal-name="SIGSEGV"')
    assert stream["text"] == "Current event: 123\n"
    assert stopped["record"] == "exec"
    assert stopped["results"]["signal-name"] == "SIGSEGV"


def test_realistic_gdb_mi_fixture_covers_runtime_context():
    fixture = Path(__file__).parent / "fixtures" / "gdb_mi_samples.log"
    lines = fixture.read_text().splitlines()
    records = parse_mi_output(lines)
    assert len(lines) == 10
    assert records[0]["results"]["stack"][0]["frame"]["func"] == "parse_packet"
    assert records[1]["results"]["locals"][0]["name"] == "cursor"
    assert len(records[2]["results"]["threads"]) == 3
    assert records[3]["results"]["reason"] == "watchpoint-trigger"
    assert records[4]["results"]["signal-name"] == "SIGSEGV"
    assert records[6]["results"]["reason"] == "breakpoint-hit"
    assert records[8]["results"]["bkpt"]["number"] == "4"
    assert records[9]["results"]["stack"][0]["frame"]["func"] == "truncated"
    assert records[9]["raw"] == lines[9]
    assert records[9]["parse_error"]


def test_malformed_mi_never_raises_and_preserves_original_text():
    malformed = ["^done,locals=[{name=\"x\",value=\"1\"", "*stopped,frame={func=",
                 "^done,broken", "~\"unterminated", "???", "[", "^done,a=\"\\q\""]
    for line in malformed:
        parsed = parse_mi_line(line)
        assert parsed is not None
        assert parsed.get("raw") == line
        assert parsed.get("parse_error")
