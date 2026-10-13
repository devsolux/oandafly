"""Run the sole reviewed full-suite command; no test-selection arguments."""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 1:
        raise ValueError("Release test runner accepts no arguments")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    import pytest
    contract = json.loads(Path(os.environ["OANDAFLY_TEST_CONTRACT"]).read_text())
    if contract["testpaths"] != ["tests"] or contract["plugins"] != ["pytest_asyncio.plugin"]:
        raise ValueError("Unsupported release test contract")
    return int(pytest.main(["-q", "-o", "addopts=", "-p", "pytest_asyncio.plugin", "-p", "scripts.release_tests",
                           "--junitxml=" + os.environ["OANDAFLY_TEST_EVIDENCE"] + ".xml", "tests"]))


if __name__ == "__main__":
    raise SystemExit(main())
