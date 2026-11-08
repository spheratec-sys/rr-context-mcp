# Development

Use Python 3.11 or newer. From the repository root, install the project and its
test dependency, then run the default mocked test suite:

```bash
python -m pip install -e '.[dev]'
pytest
```

The real rr smoke test is opt-in. On Linux with rr installed and performance
counters available, run:

```bash
RR_INTEGRATION=1 pytest tests/test_rr_integration.py
```

The integration test records `/bin/true` to a temporary trace and replays it.
The default test suite does not require rr, GDB, or performance-counter access.
Keep the MCP tool surface and Evidence contract v1 stable unless a change is
explicitly requested.
