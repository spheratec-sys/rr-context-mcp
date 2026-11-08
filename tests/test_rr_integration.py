import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.environ.get("RR_INTEGRATION") != "1", reason="set RR_INTEGRATION=1 to use real rr")
def test_real_rr_record_and_replay_smoke(tmp_path):
    rr = shutil.which("rr")
    if not rr:
        pytest.skip("rr is not installed")
    trace = Path(tmp_path) / "rr-smoke"
    record = subprocess.run([rr, "record", "-o", str(trace), "--", "/bin/true"],
                            text=True, capture_output=True, timeout=120)
    assert record.returncode == 0, record.stdout + record.stderr
    replay = subprocess.run([rr, "replay", "-a", str(trace)],
                            text=True, capture_output=True, timeout=120)
    assert replay.returncode == 0, replay.stdout + replay.stderr
