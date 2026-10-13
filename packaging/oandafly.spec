# Native-host onedir GUI + console helper. No onefile extraction or legacy imports.
import json
import os
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
sys.path.insert(0, str(root / "src"))
from oandafly.platforms import native_target
from oandafly.macos import profile

host = native_target()
requested = os.environ.get("OANDAFLY_BUILD_TARGET", host.key)
if requested != host.key:
    raise RuntimeError("PyInstaller builds require a matching native host; cross-compilation is not supported")
identity = json.loads((root / "packaging/application.json").read_text(encoding="utf-8"))
project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
if (identity["name"], identity["version"]) != (project["name"], project["version"]):
    raise RuntimeError("Desktop identity differs from project metadata")

datas = collect_data_files("oandafly") + copy_metadata("oandafly")
for value in project["dependencies"] + project["optional-dependencies"]["native-vault"] + project["optional-dependencies"]["feather"]:
    requirement = Requirement(value)
    if requirement.marker is None or requirement.marker.evaluate():
        datas += copy_metadata(requirement.name, recursive=True)
datas += [(str(root / "NOTICE.md"), "notices"), (str(root / "packaging/application.json"), "release")]
datas += [(str(root / "docs/DESKTOP.md"), "help"), (str(root / "docs/ENGINEERING_STATUS.md"), "help")]
hidden = collect_submodules("oandafly") + ["tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox",
                                         "pyarrow", "numpy", "scipy.sparse", "threadpoolctl"]
if sys.platform == "win32":
    hidden += ["win32api", "win32con", "win32security", "ntsecuritycon", "pywintypes", "keyring.backends.Windows"]
elif sys.platform == "linux":
    hidden += ["keyring.backends.SecretService", "secretstorage", "jeepney"]
elif sys.platform == "darwin":
    hidden += ["keyring.backends.macOS"]

analysis = Analysis(
    [str(root / "packaging/entrypoint.py")], pathex=[str(root / "src")],
    binaries=[], datas=datas, hiddenimports=hidden,
    excludes=["pytest", "ruff", "PyInstaller", "IPython", "matplotlib"],
    noarchive=False, optimize=0,
    # Qualification hashes real Python sources. Never allow an empty digest from
    # an archive-only frozen package; copy sources alongside the PYZ as well.
    module_collection_mode={"oandafly": "pyz+py"},
)
archive = PYZ(analysis.pure)
shared = dict(exclude_binaries=True, debug=False, strip=False, upx=False,
              argv_emulation=False, target_arch="arm64" if sys.platform == "darwin" else None,
              # Assembly is always locally ad-hoc sealed. Developer ID signing
              # happens explicitly, inside-out, only in the signed trust stage.
              codesign_identity=None,
              entitlements_file=None, disable_windowed_traceback=False)
icon = str(root / "src/oandafly/resources/icons/oandafly.ico") if sys.platform == "win32" else None
version = str(root / "packaging/windows/version-info.txt") if sys.platform == "win32" else None
gui = EXE(archive, analysis.scripts, [], name="oandafly", console=False, icon=icon, version=version, **shared)
cli = EXE(archive, analysis.scripts, [], name="oandafly-cli", console=True, icon=icon, version=version, **shared)
collection = COLLECT(gui, cli, analysis.binaries, analysis.datas, strip=False, upx=False, name="oandafly")
if sys.platform == "darwin":
    app = BUNDLE(collection, name="Oandafly.app",
        icon=str(root / "src/oandafly/resources/icons/oandafly.icns"),
        bundle_identifier=identity["identifier"],
        info_plist={"CFBundleName": "Oandafly", "CFBundleDisplayName": "Oandafly",
                    "CFBundleShortVersionString": identity["version"], "CFBundleVersion": identity["version"],
                    "LSApplicationCategoryType": "public.app-category.finance", "LSMinimumSystemVersion": profile()["application_minimum"],
                    "NSHighResolutionCapable": True, "LSBackgroundOnly": False, "LSUIElement": False})
