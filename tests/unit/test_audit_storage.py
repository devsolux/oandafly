"""Audit storage contract; the package/advisory subprocess is an offline fixture."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

from oandafly import paths


def test_auditor_still_runs_without_an_installed_project_or_site_packages():
    script = Path(__file__).resolve().parents[2] / "scripts" / "audit_dependencies.py"
    result = subprocess.run([sys.executable, "-I", "-B", "-S", str(script)],
                            env={**os.environ, "PATH": ""}, capture_output=True, text=True,
                            timeout=10, check=False)
    assert result.returncode == 1
    assert result.stderr.strip() == "uv is required to run pip-audit outside the project environment"


def test_auditor_keeps_caches_tools_and_raw_logs_under_application_root(monkeypatch, tmp_path):
    project = tmp_path / "checkout"
    (project / "docs").mkdir(parents=True)
    (project / "requirements.lock").write_text("httpx==0.28.1\n")
    (project / "uv.lock").write_text("version = 1\n")
    storage = tmp_path.resolve() / "home" / ".devsolux" / "oandafly"
    monkeypatch.setattr(paths, "application_directory", lambda: storage)
    script = Path(__file__).resolve().parents[2] / "scripts" / "audit_dependencies.py"
    main = runpy.run_path(str(script))["main"]
    monkeypatch.setitem(main.__globals__, "__file__", str(project / "scripts" / "audit_dependencies.py"))
    monkeypatch.setattr(main.__globals__["shutil"], "which", lambda name: "/fixture/uv")
    monkeypatch.setattr(sys, "argv", [str(script)])
    calls = []

    def audit(command, **kwargs):
        calls.append(command)
        assert command[command.index("--cache-dir") + 1] == str(storage / "cache" / "pip-audit")
        environment = kwargs["env"]
        assert environment["UV_CACHE_DIR"] == str(storage / "cache" / "uv")
        assert environment["UV_TOOL_DIR"] == str(storage / "cache" / "audit-tools")
        assert environment["TMPDIR"] == str(storage / "tmp")
        report = Path(command[command.index("--output") + 1])
        assert report.parent == storage / "logs" / "dependency-audit"
        report.write_text(json.dumps({"dependencies": [{"name": "httpx", "version": "0.28.1", "vulns": []}]}))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(main.__globals__["subprocess"], "run", audit)
    main()
    assert len(calls) == 1
    assert len(list((storage / "logs" / "dependency-audit").glob("*-summary.json"))) == 1
    assert not list((project / "docs").iterdir())
    assert sorted(item.name for item in project.iterdir()) == ["docs", "requirements.lock", "uv.lock"]
