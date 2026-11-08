# rr-context-mcp

An open-source, local MCP server that gives coding agents a deterministic runtime record they can inspect and replay while diagnosing bugs. This first MVP uses Mozilla's `rr` engine and the official Python MCP SDK.

## Existing-project review

This is a new implementation, not a fork. Both reference projects establish that rr can be made accessible to agents through MCP, so the useful design question is how to make the root-cause path clear and reliable.

| Project | What it does well | Design lesson for this MVP |
|---|---|---|
| [jnjaeschke/rr-mcp](https://github.com/jnjaeschke/rr-mcp) | Broad debugger coverage: multi-process replay sessions, reverse execution, breakpoints/watchpoints/catchpoints, session resources, and a detailed guide. Its reported 48 specialized tools make the GDB surface comprehensive. | Avoid presenting every GDB operation as an equally important starting point. Expose a compact investigation loop first; grow into multi-process and advanced breakpoint workflows after the basic loop is stable. |
| [karellen/karellen-rr-mcp](https://github.com/karellen/karellen-rr-mcp) | An end-to-end record, replay, and inspect flow. Uses GDB/MI to support persistent reverse-debugging sessions and has practical installation and rr troubleshooting guidance. | Keep an actual persistent debugger session and prioritize recording a failure, inspecting state, and moving backward. Avoid requiring a separate agent to manually orchestrate dozens of low-level steps for common cases. |

### Proposed architecture

```text
Coding agent (Claude Code / Cursor / other MCP host)
       │ MCP stdio
       ▼
MCP tool layer (workflow-oriented, typed, small outputs)
       │
       ├── Capture adapter ── argv-only `rr record`
       ├── Trace catalog ──── local rr trace directories
       └── Replay adapter ─── `rr replay -s` + persistent GDB/MI2
                                  │
                                  └── structured evidence: stop, stack, locals, threads
```

The tool surface is deliberately small for an MVP:

| Tool | Job in an investigation |
|---|---|
| `rr_record` | Capture the exact command and arguments that reproduce the failure. |
| `rr_environment` | Check rr, GDB, Linux, and likely performance-counter readiness. |
| `rr_list_traces` | Find existing recordings. |
| `rr_replay_start` | Connect rr's replay server to the matching executable and debug symbols. |
| `rr_inspect` | Return the stable Evidence contract: current frame, stack, locals, threads, stop reason, and position. |
| `rr_root_cause_hint` | Suggest investigation steps for crashes, assertions, wrong values, or hangs. |
| `rr_when` | Attach rr event and current-thread tick coordinates to an observation. |
| `rr_move` | Move forward or backward to bracket the event that caused the bad state. |
| `rr_interrupt` | Interrupt a currently running continue or reverse-continue operation. |
| `rr_breakpoint` | Stop at a likely causal location. |
| `rr_watchpoint` | Stop when a variable or expression is read, written, or accessed. |
| `rr_evaluate` | Inspect a single expression without exposing an arbitrary GDB console. |
| `rr_session_close` | Release the GDB and rr processes. |

### Evidence and error contracts

**Evidence contract version: v1.** This is the current stable schema for `rr_inspect` and `rr_move`.

Successful `rr_inspect` and `rr_move` calls, including dry-run mode, return the same fields:

```json
{
  "ok": true,
  "session_id": "rr-...",
  "frame": {
    "level": "0",
    "address": "0x4011a2",
    "function": "parse_packet",
    "file": "src/parser.c",
    "fullname": null,
    "line": "84"
  },
  "backtrace": [],
  "locals": [],
  "threads": [],
  "current_thread_id": "3",
  "stop_reason": {
    "reason": "signal-received",
    "signal_name": "SIGSEGV",
    "signal_meaning": "Segmentation fault",
    "thread_id": "3",
    "stopped_threads": "all",
    "frame": null,
    "details": {}
  },
  "position": {"event": 481, "ticks": 90214},
  "parse_warnings": []
}
```

Frame objects use fixed fields: `level`, `address`, `function`, `file`, `fullname`, and `line`; unavailable values are `null`. `stop_reason` uses fixed fields and retains stop-specific MI fields in `details`. `position` always contains numeric-or-null `event` and `ticks`. `parse_warnings` is empty for complete MI records. For incomplete records it contains `{ "parse_error": "...", "raw": "original MI line" }`, while any successfully parsed evidence remains available in its regular field.

`backtrace`, `locals`, and `threads` are always arrays, including when GDB provides no entries. `frame` may be `null` when no frame is available. Live and dry-run paths use the same frame and stop-reason field names.

All tool errors use this shape, with `code` identifying invalid input, unavailable runtime, timeout, a stopped target interrupt request, or an unexpected internal error:

```json
{ "ok": false, "error": { "code": "timeout", "message": "..." } }
```

Root-cause analysis should move from the observed failure toward the first event that violates an invariant. Agents can inspect at the crash, form a hypothesis, set a watchpoint on a value that should remain stable, and reverse-step or reverse-continue to identify the earlier mutation. Event/tick coordinates make observations easier to compare and cite. A future version can add direct event seeking, process selection, durable trace metadata, and automated evidence summaries without bloating the initial tool list.

### MVP limits

- rr is Linux-only in normal use and has CPU/kernel/performance-counter requirements. Some virtual machines work only when performance counters are passed through.
- Recording captures the target process tree's execution. It does not capture arbitrary remote services or all machine state.
- Replay needs the executable that matches the recording. Debug symbols improve the quality of stack and local-variable evidence.
- The server can keep multiple replay sessions open in one MCP process, but process selection within a multi-process trace, checkpoints, and trace deletion are not implemented yet.
- The current execution wait is bounded by configurable forward/reverse timeouts; expired waits return a structured `timeout` error. `rr_interrupt` can send an asynchronous interrupt while a movement command is running.
- GDB/MI results are parsed into structured frames, locals, threads, stop reasons, and event/tick coordinates. Variable inspection still depends on symbols and the executable matching the trace.
- Expected tool failures are returned as JSON objects with `ok: false` and an error code/message. `rr_environment` reports missing binaries and a best-effort performance-counter check; only a real recording can confirm the host's full rr permissions.
- `rr_record` accepts an argv array and never runs a shell command. Build steps and shell pipelines must be represented by a wrapper executable/script or run separately before recording.

## Requirements

- Linux supported by rr
- Python 3.11+
- `rr` and `gdb` on `PATH`
- rr hardware performance counter access configured for your environment

Check rr independently before connecting the MCP server. Replay opens the interactive debugger, so exit it after the smoke test:

```bash
rr record -o /tmp/rr-smoke -- /bin/true && rr replay /tmp/rr-smoke
```

rr may not work inside containers or VMs without the necessary performance-counter access. Consult the rr installation guide for your CPU and distribution.

## Install from this checkout

Install the runtime package with pip:

```bash
python -m pip install .
rr-context-mcp
```

For development, install the optional test dependencies in an editable checkout:

```bash
python -m pip install -e '.[dev]'
pytest
```

Or install the local command into an isolated pipx environment:

```bash
pipx install .
rr-context-mcp
```

After a release is published to PyPI, users can run `pipx run rr-context-mcp` or `uvx rr-context-mcp` without installing it into a project environment.

On Windows, the venv activation command is `.venv\\Scripts\\activate`; the server itself still requires Linux and rr.

Run the mocked unit and MCP protocol tests:

```bash
pytest
```

The real rr smoke test is opt-in and requires Linux, rr, and working performance counters:

```bash
RR_INTEGRATION=1 pytest tests/test_rr_integration.py
```

Launch the stdio server directly:

```bash
rr-context-mcp
```

You can override rr's standard recording directory (`~/.local/share/rr`) with `RR_TRACE_DIR=/path/to/traces`.

Exercise the full MCP handshake and a sample crash-analysis workflow without rr/GDB:

```bash
rr-context-mcp --dry-run
```

The demo server does not record or replay an actual program. Its sample workflow starts at a SIGSEGV, returns full evidence, installs a cursor watchpoint, simulates a reverse continue to the earlier write, and produces a watchpoint-specific root-cause hint. Set `RR_INTEGRATION=1` when running tests to opt into a real rr record/replay smoke test; the default suite remains fully mocked.

## Claude Code configuration

From the repository root, use an absolute checkout path:

```bash
claude mcp add --transport stdio rr-context-mcp -- /absolute/path/to/rr-context-mcp/.venv/bin/rr-context-mcp
```

Or add a project-local `.mcp.json`:

```json
{
  "mcpServers": {
    "rr-context-mcp": {
      "type": "stdio",
      "command": "/absolute/path/to/rr-context-mcp/.venv/bin/rr-context-mcp",
      "env": {
        "RR_TRACE_DIR": "/home/you/.local/share/rr"
      }
    }
  }
}
```

## Cursor configuration

Add a project `.cursor/mcp.json` (or use Cursor's MCP settings UI):

```json
{
  "mcpServers": {
    "rr-context-mcp": {
      "command": "/absolute/path/to/rr-context-mcp/.venv/bin/rr-context-mcp",
      "env": {
        "RR_TRACE_DIR": "/home/you/.local/share/rr"
      }
    }
  }
}
```

Restart or reload the MCP connection after changing configuration. For other stdio MCP clients, configure the same executable and environment.

## Typical agent workflow

1. Build the program with debug symbols and reproduce the problem once with `rr_record`, passing the executable and arguments separately.
2. Start replay with the returned `trace_path` and the matching executable.
3. Inspect the stop, stack, locals, and threads. Set a breakpoint or evaluate the suspect value.
4. Use `rr_move` with `reverse_step` or `reverse_continue` to identify when the incorrect value was created; move forward to confirm the causal path.
5. Report the observed evidence and close the session.

The server includes an `rr://guide` resource with this workflow.

## Development status

This is a runnable MVP. It includes the official MCP SDK, tolerant GDB/MI parsing with partial data and original text for malformed records, fully structured inspect/move evidence, stale-stop-resistant bounded execution waits, asynchronous interrupts, multiple replay sessions, capability diagnostics, watchpoints, symptom-specific investigation hints, and a demo mode. Ten realistic MI fixture lines cover stacks, locals, threads, watchpoints, signals, breakpoints, stream output, and truncated data. Unit and protocol tests run without rr hardware. Real capture/replay remains host-dependent and is covered by an opt-in `RR_INTEGRATION=1` smoke test.

## License

MIT
