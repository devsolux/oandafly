#!/usr/bin/env python3
"""Check source completeness without importing the worker or contacting a broker.

The manifest covers source-distribution content, not an installed environment,
external datasets, operator credentials, model qualification or live readiness.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "PACKAGE_MANIFEST.json"
TOP_FILES = frozenset({
    ".gitignore", ".python-version", "README.md", "NOTICE.md", "CHANGELOG.md", "LICENSE",
    "AGENTS.md", "CLAUDE.md", "MANIFEST.in", "Makefile", "pyproject.toml", "requirements-build.in", "dev.sh", "dev.ps1", ".gitattributes",
    "uv.lock", "requirements.lock", "requirements-dev.lock", "requirements-build.lock",
})
TOP_DIRS = frozenset({"src", "tests", "scripts", "docs", "examples", ".github", "deploy", "packaging"})
EXCLUDED_PARTS = frozenset({
    "__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".venv",
    "node_modules", "build", "dist", "private", "credentials", "datasets", "models",
})
ALLOWED_SUFFIXES = frozenset({
    ".py", ".md", ".json", ".xml", ".txt", ".template", ".png", ".ico", ".icns",
    ".yml", ".yaml", ".toml", ".in", ".service", ".sh", ".ps1", ".spec", ".plist",
    ".swift", ".m", ".mm", ".c", ".h", ".entitlements", ".pbxproj", ".xcscheme", ".xcconfig",
})
RELEASE_FILES = ("uv.lock", "requirements.lock", "requirements-dev.lock", "requirements-build.lock")
REQUIRED_FILES = frozenset({
    "pyproject.toml", "README.md", "NOTICE.md", "CHANGELOG.md", "MANIFEST.in", "Makefile",
    ".gitignore", ".python-version", ".github/workflows/ci.yml", "requirements-build.in",
    "src/oandafly/__init__.py", "src/oandafly/__main__.py", "src/oandafly/py.typed",
    "src/oandafly/worker/application.py", "src/oandafly/neural/runtime.py",
    "scripts/project_integrity.py", "scripts/package_source.py", "scripts/check_distribution.py",
    "scripts/lock_dependencies.py", "scripts/check_test_evidence.py", "scripts/export_schemas.py",
    "scripts/build_sqlite.py", "scripts/audit_dependencies.py",
    "examples/worker.practice.json.template", "examples/worker.shadow.json.template",
    "examples/worker.live.json.template", "examples/training-spec.json.template",
    "examples/cost-assumptions.json.template", "examples/trust-policy.json.template",
    "deploy/systemd/oandafly.service.template", "deploy/systemd/README.md",
    "docs/PROJECT_COMPLETENESS.md", "docs/DEPENDENCIES.md", "docs/ARTIFACTS.md",
    "docs/BOOTSTRAP.md", "docs/ENGINEERING_STATUS.md", "docs/OPERATIONS.md",
    "tests/packaging/test_project_integrity.py", "dev.sh", "dev.ps1", ".gitattributes",
    "src/oandafly/desktop.py", "src/oandafly/platforms.py", "src/oandafly/ui/app.py",
    "src/oandafly/ui/controller.py", "src/oandafly/ui/model.py", "src/oandafly/ui/verification.py",
    "scripts/dev.py", "scripts/build_desktop.py", "scripts/build_contracts.py",
    "scripts/verify_build.py", "scripts/native_window.py", "packaging/application.json",
    "packaging/entrypoint.py", "packaging/oandafly.spec", "packaging/README.md",
    "packaging/windows/version-info.txt", "packaging/windows/install-shortcut.ps1",
    "packaging/linux/install.sh", "packaging/linux/com.devsolux.oandafly.desktop.template",
    "docs/DESKTOP.md", "tests/desktop/test_contracts.py", "tests/desktop/test_gui.py",
    ".github/workflows/desktop.yml",
    "src/oandafly/macos.py", "src/oandafly/resources/macos-profile.json", "src/oandafly/neural/containment.py",
    "src/oandafly/runtime_probe.py",
    "scripts/macos_profile.py", "scripts/macos_signing.py", "scripts/macho_audit.py",
    "scripts/release_policy.py", "scripts/release_tests.py", "scripts/run_release_tests.py",
    "scripts/check_quality.py", "scripts/release_artifacts.py", "scripts/baseline_acceptance.py",
    "scripts/release_doctor.py", "packaging/release-tests.json", "docs/PRODUCTION_BUILD.md",
    "tests/packaging/test_production_profiles.py",
})
SCHEMAS = (
    "AuthorizationGrant", "CandidateReference", "CostAssumptions", "CredentialReference",
    "DatasetReview", "DeploymentApproval", "EligibilityEvidence", "GraphManifest", "KernelConfig",
    "ModelManifest", "ModelValidation", "RiskLimits", "SignedDocument", "TrainingSpec",
    "TrustPolicy", "VerifiedAccountProfile", "WorkerConfig",
)


def source_files(root: Path = ROOT) -> list[Path]:
    """Select distributable files. Runtime databases, arrays and secrets are excluded.

    This is a path/extension boundary, not a content-based secret detector. Keep
    credentials and personal data outside every source directory.
    """
    root = root.absolute()
    selected = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in relative.parts):
            continue
        eligible = relative.as_posix() in TOP_FILES or (
            relative.parts[0] in TOP_DIRS and
            (path.suffix in ALLOWED_SUFFIXES or path.name == "py.typed")
        )
        if path.is_symlink() and (eligible or relative.parts[0] in TOP_DIRS):
            raise ValueError(f"Symbolic link in source content: {relative.as_posix()}")
        if eligible and path.is_file():
            selected.append(path)
    return selected


def file_record(path: Path) -> dict[str, int | str]:
    content = path.read_bytes()
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def create_manifest(root: Path = ROOT) -> dict:
    root = root.absolute()
    files = {p.relative_to(root).as_posix(): file_record(p) for p in source_files(root)}
    return {
        "format": "oandafly.source-manifest.1", "application_version": "1.0.0",
        "scope": "Selected source files; this manifest excludes itself. Not a trading qualification.",
        "file_count_excluding_manifest": len(files), "files": files,
    }


def write_manifest(root: Path = ROOT) -> dict:
    if (root / MANIFEST_NAME).is_symlink():
        raise ValueError("Refusing to overwrite a symlinked source manifest")
    manifest = create_manifest(root)
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def verify_manifest(root: Path = ROOT) -> list[str]:
    path = root / MANIFEST_NAME
    if not path.is_file() or path.is_symlink():
        return [f"Missing regular {MANIFEST_NAME}"]
    try:
        declared = json.loads(path.read_text(encoding="utf-8"))
        actual = create_manifest(root)
        if declared.get("format") != actual["format"] or declared.get("application_version") != "1.0.0":
            return ["Unrecognized manifest format/version"]
        expected = declared["files"]
        if not isinstance(expected, dict):
            return ["Manifest file map is invalid"]
        issues = [f"Missing manifested file: {p}" for p in sorted(set(expected) - set(actual["files"]))]
        issues += [f"Unmanifested source file: {p}" for p in sorted(set(actual["files"]) - set(expected))]
        issues += [f"Changed manifested file: {p}" for p in sorted(set(expected) & set(actual["files"]))
                   if expected[p] != actual["files"][p]]
        if declared.get("file_count_excluding_manifest") != len(expected):
            issues.append("Manifest file count does not match its map")
        return issues
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"Unreadable manifest: {type(exc).__name__}"]


def audit(root: Path = ROOT, *, check_manifest: bool = False, require_locks: bool = False) -> dict:
    root = root.absolute()
    issues: list[str] = []
    files = source_files(root)
    paths = {p.relative_to(root).as_posix() for p in files}
    required = REQUIRED_FILES | {f"examples/schemas/{name}.schema.json" for name in SCHEMAS}
    missing = sorted(required - paths)
    issues += [f"Required source file missing: {p}" for p in missing]
    modules: set[str] = set()
    trees = {}
    for path in files:
        if path.suffix in {".py", ".spec"}:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                trees[path] = tree
                if path.is_relative_to(root / "src"):
                    modules.add(".".join(path.relative_to(root / "src").with_suffix("").parts).removesuffix(".__init__"))
            except (SyntaxError, UnicodeError) as exc:
                issues.append(f"Python syntax/encoding: {path.relative_to(root)}: {type(exc).__name__}")
    missing_imports = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [entry.name for entry in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            for name in names:
                if (name == "oandafly" or name.startswith("oandafly.")) and name not in modules:
                    missing_imports.append({"file": str(path.relative_to(root)), "line": node.lineno, "module": name})
    issues += [f"Missing internal import: {item}" for item in missing_imports]
    broken_links = []
    for path in files:
        if path.suffix == ".md":
            for href in re.findall(r"(?<!!)\[[^\]]*\]\(([^ )]+)(?: [^)]*)?\)", path.read_text(encoding="utf-8")):
                if "://" in href or href.startswith(("#", "mailto:")):
                    continue
                if not (path.parent / href.split("#", 1)[0]).exists():
                    broken_links.append({"file": str(path.relative_to(root)), "target": href})
        if path.suffix == ".json" or path.name.endswith(".json.template"):
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError) as exc:
                issues.append(f"Invalid JSON: {path.relative_to(root)}: {type(exc).__name__}")
    issues += [f"Broken local documentation link: {item}" for item in broken_links]
    try:
        metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project = metadata["project"]
        if project["name"] != "oandafly" or project["version"] != "1.0.0":
            issues.append("Project name/version drift")
        for value in {**project.get("scripts", {}), **project.get("gui-scripts", {})}.values():
            module, symbol = value.split(":", 1)
            module_path = root / "src" / (module.replace(".", "/") + ".py")
            if module not in modules or module_path not in trees or not any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol
                for node in trees[module_path].body
            ):
                issues.append(f"Missing console entry point: {value}")
        for pattern in metadata["tool"]["setuptools"]["package-data"]["oandafly"]:
            if not any((root / "src/oandafly").glob(pattern)):
                issues.append(f"Package-data pattern matches no files: {pattern}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        issues.append(f"Invalid build metadata: {type(exc).__name__}")
    missing_locks = [name for name in RELEASE_FILES if name not in paths]
    if require_locks:
        issues += [f"Unresolved release dependency file: {name}" for name in missing_locks]
    manifest_issues = verify_manifest(root) if check_manifest else []
    issues += manifest_issues
    return {
        "application_version": "1.0.0", "source_check_passed": not issues,
        "selected_files_excluding_manifest": len(files), "python_modules": len(modules),
        "missing_required_source_files": missing, "missing_internal_imports": missing_imports,
        "broken_local_markdown_links": broken_links, "manifest_checked": check_manifest,
        "missing_release_dependency_files": missing_locks,
        "dependency_resolution_verified": False, "native_trading_qualified": False,
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--write-manifest", action="store_true")
    parser.add_argument("--verify-manifest", action="store_true")
    parser.add_argument("--require-locks", action="store_true")
    args = parser.parse_args()
    if args.write_manifest:
        write_manifest(args.root)
    result = audit(args.root, check_manifest=args.verify_manifest, require_locks=args.require_locks)
    print(json.dumps(result, indent=2))
    return 0 if result["source_check_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
