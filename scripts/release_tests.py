"""Pytest qualification plugin: exact collection and outcomes, never a count floor."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


class Evidence:
    def __init__(self):
        self.collected = []
        self.selected = []
        self.excluded = {}
        self.results = {}
        self.issues = []


def pytest_configure(config):
    config._oandafly_evidence = Evidence()
    for marker in ("linux_containment", "posix_only"):
        config.addinivalue_line("markers", marker + ": reviewed platform applicability")


def pytest_collection_modifyitems(config, items):
    evidence = config._oandafly_evidence
    contract = json.loads(Path(os.environ["OANDAFLY_TEST_CONTRACT"]).read_text())
    target = os.environ["OANDAFLY_BUILD_TARGET"]
    evidence.collected = [item.nodeid for item in items]
    if len(set(evidence.collected)) != len(evidence.collected):
        evidence.issues.append("Duplicate test identities")
    selected = []
    for item in items:
        reasons = [marker for marker, targets in contract["platform_exclusions"].items()
                   if target in targets and item.get_closest_marker(marker)]
        if reasons:
            evidence.excluded[item.nodeid] = reasons
        else:
            selected.append(item)
    items[:] = selected
    evidence.selected = [item.nodeid for item in selected]
    if not selected:
        evidence.issues.append("Empty release test selection")
    if config.option.keyword or config.option.markexpr:
        evidence.issues.append("Selective test arguments are forbidden")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    evidence = item.config._oandafly_evidence
    results = evidence.results.setdefault(item.nodeid, {})
    if report.when in results:
        evidence.issues.append("Repeated test phase: " + item.nodeid)
    results[report.when] = report.outcome
    if hasattr(report, "wasxfail"):
        evidence.issues.append("Unexpected xfail/xpass: " + item.nodeid)


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    e = config._oandafly_evidence
    if set(e.results) != set(e.selected):
        e.issues.append("Executed tests differ from fresh complete collection")
    if any(result != {"setup": "passed", "call": "passed", "teardown": "passed"} for result in e.results.values()):
        e.issues.append("A required test skipped, failed, errored or did not finish")
    if session.testscollected != len(e.selected):
        e.issues.append("Unexpected deselection")
    passed = exitstatus == 0 and not e.issues
    document = {"format": "oandafly.release-test-evidence.1", "passed": passed,
                "binding": json.loads(Path(os.environ["OANDAFLY_TEST_BINDING"]).read_text()),
                "collected": e.collected, "selected": e.selected, "excluded": e.excluded,
                "results": e.results, "issues": e.issues, "exitstatus": int(exitstatus)}
    Path(os.environ["OANDAFLY_TEST_EVIDENCE"]).write_text(json.dumps(document, indent=2) + "\n")
    if not passed:
        session.exitstatus = 1
