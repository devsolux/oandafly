"""Desktop-first 1.0.0 entry. Native writes remain behind the qualified worker."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import sqlite3
import sys
from pathlib import Path

from oandafly import __version__


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="oandafly", description="Full-MaleCNS autonomous OANDA trading")
    command.add_argument("--version", action="version", version=f"oandafly {__version__}")
    commands = command.add_subparsers(dest="command")
    diagnostics = commands.add_parser("doctor", help="Report desktop and worker prerequisites separately")
    diagnostics.add_argument("--desktop", action="store_true", help="Use desktop prerequisites for the exit status")
    desktop = commands.add_parser("desktop", help="Open the native workspace; no worker starts automatically")
    desktop.add_argument("--authority")
    desktop.add_argument("--config", type=Path)
    for name, help_text in (("run", "Run the headless practice/live/shadow worker"),
                            ("validate-config", "Validate private metadata-only worker configuration")):
        child = commands.add_parser(name, help=help_text)
        child.add_argument("--config", type=Path, required=True)
    for name, help_text in (("status", "Read the worker's private status"),
                            ("halt", "Queue HALT: block alpha entries; reconciliation and protective recovery continue"),
                            ("flatten", "Queue FLATTEN: request authorized reductions toward zero; no fill guarantee"),
                            ("resume", "Queue RESUME: recheck authorization, scope, reconciliation, protection and risk"),
                            ("stop", "Queue STOP: stop the worker; does not guarantee closing positions")):
        child = commands.add_parser(name, help=help_text)
        child.add_argument("--authority", required=True, help="Exact configured authority ID")
    graph = commands.add_parser("graph-import", help="Compile all curated MaleCNS nodes and all internal published edges")
    for flag in ("neurons", "connections", "signs", "output"):
        graph.add_argument("--"+flag, type=Path, required=True)
    graph.add_argument("--release", required=True)
    for flag, default in (("neuron-column", "bodyId"), ("pre-column", "bodyId_pre"),
                          ("post-column", "bodyId_post"), ("weight-column", "weight"),
                          ("sign-id-column", "bodyId"), ("sign-column", "sign")):
        graph.add_argument("--"+flag, default=default)
    for name, help_text in (("train", "Produce one unqualified candidate without execution credentials"),
                            ("train-watch", "Retrain unqualified candidates whenever the selected history window changes")):
        child = commands.add_parser(name, help=help_text)
        for flag in ("graph", "archive", "output", "spec", "costs"):
            child.add_argument("--"+flag, type=Path, required=True)
        child.add_argument("--instrument", required=True)
        if name == "train-watch":
            child.add_argument("--interval-seconds", type=int, default=3600)
            child.add_argument("--once", action="store_true")
    identities = commands.add_parser("identities", help="Print exact source, runtime and artifact identities for external review")
    identities.add_argument("--graph", type=Path)
    identities.add_argument("--model", type=Path)
    enroll = commands.add_parser("credential-enroll", help="Prompt for a token and store it only in the selected native vault")
    enroll.add_argument("--reference", type=Path, required=True)
    keyname = commands.add_parser("credential-name", help="Derive a systemd credential name from metadata, not a token")
    keyname.add_argument("--reference", type=Path, required=True)
    keygen = commands.add_parser("approval-keygen", help="Create an encrypted Ed25519 key outside the worker")
    keygen.add_argument("--output", type=Path, required=True)
    sign = commands.add_parser("sign-approval", help="Sign an already reviewed claim; this performs no scientific/legal verification")
    sign.add_argument("--key", type=Path, required=True)
    sign.add_argument("--payload", type=Path, required=True)
    sign.add_argument("--output", type=Path, required=True)
    sign.add_argument("--key-id", required=True)
    sign.add_argument("--role", required=True, choices=("ACCOUNT_ELIGIBILITY", "TRADING_AUTHORIZATION", "DATASET_REVIEW", "MODEL_VALIDATION", "DEPLOYMENT"))
    return command


def doctor() -> dict:
    import ctypes.util
    packages = {}
    for name in ("pydantic", "httpx", "cryptography", "numpy", "scipy", "threadpoolctl", "keyring", "pyarrow"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    checks = {"python_3_13": (3, 13) <= sys.version_info < (3, 14),
              "sqlite_3_51_3_or_newer": sqlite3.sqlite_version_info >= (3, 51, 3),
              "linux_production_containment": sys.platform == "linux" and ctypes.util.find_library("seccomp") is not None,
              "core_dependencies": all(packages[n] is not None for n in ("pydantic", "httpx", "cryptography", "numpy", "scipy", "threadpoolctl"))}
    from oandafly.platforms import host_report
    host = host_report()
    try:
        import tkinter
        tk_version = tkinter.TkVersion
    except ImportError:
        tk_version = None
    desktop_checks = {"native_target": host["native_desktop_target"], "python_3_13": checks["python_3_13"],
                      "tk_import": tk_version is not None, "core_dependencies": checks["core_dependencies"]}
    return {"version": __version__, "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "desktop": {"checks": desktop_checks, "tk": tk_version,
                        "prerequisites_satisfied": all(desktop_checks.values()), "window_tested": False},
            "host": host,
            "platform": platform.platform(), "packages": packages, "checks": checks,
            "runtime_prerequisites_satisfied": all(checks.values()),
            "trading_qualified": False,
            "note": "Runtime prerequisites are not dataset, model, account, credential or deployment qualification."}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        from oandafly.macos import require_runtime
        require_runtime()
        if args.command in (None, "desktop"):
            from oandafly.ui.app import launch
            launch(getattr(args, "authority", None), getattr(args, "config", None))
            return 0
        if args.command == "doctor":
            result = doctor()
            print(json.dumps(result, indent=2))
            return 0 if (result["desktop"]["prerequisites_satisfied"] if args.desktop else result["runtime_prerequisites_satisfied"]) else 2
        if args.command == "run":
            from oandafly.platforms import WORKER_PLATFORM_FENCE, local_worker_available
            if not local_worker_available():
                print(json.dumps({"status": "BLOCKED", "code": "UNSUPPORTED_WORKER_PLATFORM",
                                  "message": WORKER_PLATFORM_FENCE}), file=sys.stderr)
                return 2
            from oandafly.paths import configure_runtime_storage
            configure_runtime_storage()  # before importing runtime services, per its contract
            from oandafly.worker.application import run_worker
            run_worker(args.config)
            return 0
        if args.command == "validate-config":
            from oandafly.worker.configuration import load_config
            value = load_config(args.config)
            print(json.dumps({"valid_schema": True, "mode": value.mode, "environment": value.environment,
                              "authorization_verified": False}, indent=2))
        elif args.command in {"status", "halt", "flatten", "resume", "stop"}:
            from oandafly.worker.control import read_status, send_control
            if args.command == "status":
                print(json.dumps(read_status(args.authority), indent=2))
            else:
                print(json.dumps({"request_id": send_control(args.authority, args.command.upper()),
                                  "state": "QUEUED_NOT_YET_ACKNOWLEDGED"}))
        elif args.command == "graph-import":
            from oandafly.neural.importer import ImportColumns, compile_graph
            columns = ImportColumns(neuron_id=args.neuron_column, pre_id=args.pre_column, post_id=args.post_column,
                                    contacts=args.weight_column, sign_id=args.sign_id_column, sign=args.sign_column)
            graph = compile_graph(neurons_path=args.neurons, connections_path=args.connections,
                                  signs_path=args.signs, destination=args.output, release=args.release,
                                  scope="FULL_DECLARED_MALECNS", columns=columns)
            print(graph.model_dump_json(indent=2))
        elif args.command in {"train", "train-watch"}:
            from oandafly.security.attestations import private_document
            from oandafly.training.trainer import CostAssumptions, TrainingSpec, train_candidate
            spec = TrainingSpec.model_validate_json(private_document(args.spec))
            costs = CostAssumptions.model_validate_json(private_document(args.costs))
            if args.command == "train-watch":
                from oandafly.training.watch import watch_training
                watch_training(graph=args.graph, archive=args.archive, instrument=args.instrument,
                               output=args.output, spec=spec, costs=costs,
                               interval_seconds=args.interval_seconds, once=args.once)
            else:
                result = train_candidate(graph_directory=args.graph, archive_directory=args.archive,
                    instrument=args.instrument, destination=args.output, spec=spec, costs=costs)
                print(json.dumps(result, indent=2))
        elif args.command == "identities":
            from oandafly.neural.qualification import source_identity, runtime_identity
            from oandafly.neural.artifacts import GraphManifest, ModelManifest, manifest_document
            result = {"source_sha256": source_identity(), "runtime_sha256": runtime_identity()}
            for name, cls in (("graph", GraphManifest), ("model", ModelManifest)):
                path = getattr(args, name)
                if path is not None:
                    result[name+"_sha256"] = cls.model_validate_json(manifest_document(path / "manifest.json")).fingerprint
            print(json.dumps(result, indent=2))
        elif args.command in {"credential-enroll", "credential-name"}:
            from oandafly.security.attestations import private_document
            from oandafly.security.credentials import CredentialReference
            reference = CredentialReference.model_validate_json(private_document(args.reference))
            if args.command == "credential-name":
                from oandafly.security.linux_vault import SystemdCredentialVault
                print(SystemdCredentialVault.credential_name(reference))
            else:
                from getpass import getpass
                from pydantic import SecretStr
                from oandafly.security.vault import native_vault
                reference.authorize(purpose=reference.purpose, environment=reference.environment)
                native_vault().store(reference, SecretStr(getpass("Scoped OANDA token (not echoed): ")))
                print("Credential stored in the native vault. No account was armed.")
        elif args.command in {"approval-keygen", "sign-approval"}:
            from oandafly.security.signing import create_key, sign_claim
            if args.command == "approval-keygen":
                print(json.dumps({"public_key_hex": create_key(args.output)}, indent=2))
            else:
                print(json.dumps({"document_sha256": sign_claim(key_path=args.key, payload_path=args.payload,
                    output=args.output, key_id=args.key_id, role=args.role)}, indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Operational errors are redacted; logs never print native response text,
        # secrets, arbitrary paths or exception reprs containing credentials.
        print(json.dumps({"status": "BLOCKED", "code": type(exc).__name__,
                          "message": "Operation did not complete. Inspect the documented prerequisites and private worker status."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
