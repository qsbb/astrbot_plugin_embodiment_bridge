from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_operator_diagnostic_scroll_position_with_real_dom_harness() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = Path(__file__).with_name("operator_dom_harness.mjs")
    result = subprocess.run(
        [node, str(harness)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout
