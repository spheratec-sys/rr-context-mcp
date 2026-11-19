# Changelog

## 0.1.0

- Initial alpha release of the rr-backed MCP server for deterministic runtime debugging.
- Added the Evidence contract v1 for structured frames, backtraces, locals, threads, stop reasons, and execution positions.
- Added dry-run mode with a sample segfault, inspection, watchpoint, reverse-execution, and root-cause-hint workflow.
- Added replay controls including interrupt, breakpoints, watchpoints, expression evaluation, and bounded movement timeouts.
- Standardized tool failures as structured errors with stable codes and messages.
- Added defensive GDB/MI parsing that preserves partial evidence and parse warnings for incomplete records.
- Added mocked tests and an opt-in real rr smoke test using `RR_INTEGRATION=1`.
