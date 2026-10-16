"""Cross-platform desktop contracts. No display, broker, or OS vault needed."""
from __future__ import annotations

import ast
import hashlib
import struct
import sys
import tomllib
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from oandafly.__main__ import parser
from oandafly.platforms import native_target
from oandafly.ui.controller import VerificationBackend, worker_command
from oandafly.ui.model import command_confirmation, status_view, validate_authority
from oandafly.worker.control import authority_storage_name
from scripts.build_contracts import binary_kind, bundle_paths, identity, inspect_bundle
from scripts.build_desktop import build, build_lock, package_native, plan
from scripts.project_integrity import ROOT, source_files
from scripts.verify_build import clean_environment, executable_path


@pytest.mark.parametrize('system,machine,target', [
    ('darwin','arm64','macos-arm64'), ('win32','AMD64','windows-amd64'),
    ('linux','x86_64','linux-amd64'), ('linux','amd64','linux-amd64'),
])
def test_explicit_native_targets(system, machine, target):
    assert native_target(system, machine, bits=64).key == target


@pytest.mark.parametrize('system,available', [('linux',True),('darwin',False),('win32',False)])
def test_local_worker_is_linux_only(system, available):
    from oandafly.platforms import local_worker_available
    assert local_worker_available(system) is available


def test_cli_run_is_fenced_off_linux_before_touching_configuration(monkeypatch, capsys, tmp_path):
    import json
    from oandafly import __main__, platforms
    from oandafly.domain.errors import SafetyFence
    from oandafly.worker.application import run_worker
    monkeypatch.setattr(platforms, 'local_worker_available', lambda system=None: False)
    assert __main__.main(['run', '--config', str(tmp_path / 'absent.json')]) == 2
    assert json.loads(capsys.readouterr().err)['code'] == 'UNSUPPORTED_WORKER_PLATFORM'
    with pytest.raises(SafetyFence, match='Linux'):
        run_worker(tmp_path / 'absent.json')


@pytest.mark.parametrize('system,machine,bits', [
    ('darwin','x86_64',64), ('linux','aarch64',64), ('win32','arm64',64),
    ('win32','AMD64',32), ('linux','i686',32), ('freebsd','x86_64',64),
])
def test_unsupported_native_targets_are_not_claimed(system, machine, bits):
    with pytest.raises(ValueError):
        native_target(system, machine, bits=bits)


def test_no_arguments_open_desktop_not_worker(monkeypatch):
    from oandafly import __main__
    calls = []
    monkeypatch.setattr('oandafly.ui.app.launch', lambda *values: calls.append(values))
    assert __main__.main([]) == 0
    assert calls == [(None, None)]


def test_desktop_does_not_require_authority_or_config():
    args = parser().parse_args(['desktop'])
    assert args.authority is args.config is None


def test_headless_worker_still_requires_config():
    with pytest.raises(SystemExit):
        parser().parse_args(['run'])


def test_separate_gui_and_console_entrypoints():
    project = tomllib.loads((ROOT/'pyproject.toml').read_text())['project']
    assert project['scripts']['oandafly'] == 'oandafly.__main__:main'
    assert project['gui-scripts']['oandafly-desktop'] == 'oandafly.desktop:main'
    assert project['requires-python'] == '>=3.13,<3.14'
    assert project['optional-dependencies']['desktop-build'] == ['pyinstaller==6.22.3']
    assert 'research' not in project['optional-dependencies']


def test_packaging_identity_is_unarmed_and_current():
    value = identity()
    assert value['entrypoint'] == 'oandafly.desktop:main'
    assert value['default_command'] == 'desktop'
    assert value['automatic_worker_start'] is value['default_armed'] is False


@pytest.mark.parametrize('bad', ['', '.', '..', '/tmp', 'a/b', 'a\\b', ' hello', 'x'*129])
def test_authority_validation_rejects_unsafe_values(bad):
    with pytest.raises(ValueError):
        validate_authority(bad)
    with pytest.raises(ValueError):
        authority_storage_name(bad)


@pytest.mark.parametrize('value', ['practice-1', 'A:B', 'CON', 'a.', 'UPPER', 'upper'])
def test_authority_storage_is_portable_and_exact(value):
    assert validate_authority(value) == value
    name = authority_storage_name(value)
    assert name == hashlib.sha256(value.encode()).hexdigest()
    assert len(name) == 64 and ':' not in name


def test_case_distinct_authorities_do_not_collide_on_windows():
    assert authority_storage_name('Account') != authority_storage_name('account')


def test_status_read_has_no_directory_creation(monkeypatch, tmp_path):
    import oandafly.worker.control as control
    monkeypatch.setattr(control, 'application_directory', lambda: tmp_path/'absent')
    monkeypatch.setattr(control, 'state_directory', lambda: pytest.fail('status read initialized state'))
    with pytest.raises(FileNotFoundError):
        control.read_status('test')
    assert not (tmp_path/'absent').exists()


def test_status_rejects_cross_authority(monkeypatch, tmp_path):
    import oandafly.worker.control as control
    monkeypatch.setattr(control, 'application_directory', lambda: tmp_path)
    monkeypatch.setattr(control, 'private_document', lambda path: b'{"authority_id":"different"}')
    with pytest.raises(ValueError, match='cross-authority'):
        control.read_status('test')


def status_body(when):
    return {'authority_id':'test', 'updated_at':when.isoformat(), 'mode':'SHADOW', 'environment':'SANDBOX', 'safety':'BLOCKED'}


def test_status_never_converts_stale_or_future_observations_to_ready():
    now = datetime.now(timezone.utc)
    assert status_view(status_body(now), 'test', now=now)['freshness'] == 'CURRENT OBSERVATION'
    for stamp in (now-timedelta(seconds=16), now+timedelta(seconds=2)):
        assert 'STALE' in status_view(status_body(stamp), 'test', now=now)['freshness']


def test_status_requires_matching_authority_and_timezone():
    with pytest.raises(ValueError):
        status_view(status_body(datetime.now(timezone.utc)), 'other')
    with pytest.raises(ValueError):
        status_view(status_body(datetime(2026,1,1)), 'test')


def test_stop_is_not_flatten_and_resume_does_not_create_authority():
    assert 'NOT a flatten' in command_confirmation('STOP')
    assert 'can place orders' in command_confirmation('FLATTEN')
    assert 'existing grants' in command_confirmation('RESUME')
    with pytest.raises(ValueError):
        command_confirmation('ARM_LIVE')


def test_frozen_children_use_console_helper_not_python_flags(tmp_path):
    exe = tmp_path/'oandafly.exe'
    command = worker_command(tmp_path/'worker with spaces.json', executable=str(exe), frozen=True)
    assert Path(command[0]).name == 'oandafly-cli.exe'
    assert command[1:3] == ['run','--config']
    assert '-m' not in command and '-I' not in command
    assert command[-1].endswith('worker with spaces.json')


def test_source_children_use_isolated_module_entry(tmp_path):
    command = worker_command(tmp_path/'worker.json', executable=str(tmp_path/'python'), frozen=False)
    assert command[1:5] == ['-I','-m','oandafly','run']


@pytest.mark.parametrize('operation', ['status','start','configuration','control'])
def test_verification_backend_cannot_access_accounts(operation):
    with pytest.raises(RuntimeError):
        getattr(VerificationBackend(), operation)


def test_missing_windowed_streams_are_safe(monkeypatch):
    from oandafly.desktop import ensure_standard_streams
    monkeypatch.setattr(sys, 'stdout', None)
    monkeypatch.setattr(sys, 'stderr', None)
    monkeypatch.setattr(sys, 'stdin', None)
    ensure_standard_streams()
    handles = [sys.stdout, sys.stderr, sys.stdin]
    assert all(handle is not None for handle in handles)
    for handle in handles:
        handle.close()


def test_freeze_support_precedes_application_import():
    text = (ROOT/'packaging/entrypoint.py').read_text()
    assert text.index('multiprocessing.freeze_support()') < text.index('from oandafly.desktop import main')


def test_no_removed_modules_or_backend_signing_defaults():
    combined = '\n'.join((ROOT/name).read_text() for name in ('dev.sh','dev.ps1','packaging/oandafly.spec','scripts/dev.py'))
    for removed in ('customtkinter', 'optuna', 'polars', '--demo', '--self-check', '--extra research'):
        assert removed not in combined
    assert 'MACOS_SIGN_IDENTITY:-' not in (ROOT/'scripts/dev.py').read_text()


def test_spec_collects_source_as_well_as_pyz():
    text = (ROOT/'packaging/oandafly.spec').read_text()
    ast.parse(text)
    assert '"oandafly": "pyz+py"' in text
    assert 'console=False' in text and 'console=True' in text
    assert 'oandafly-cli' in text
    assert 'exclude_binaries=True' in text
    assert 'COLLECT(' in text


def test_packager_retains_all_desktop_inputs():
    selected = {p.relative_to(ROOT).as_posix() for p in source_files(ROOT)}
    required = {'dev.sh','dev.ps1','.gitattributes','packaging/oandafly.spec','packaging/entrypoint.py',
                'packaging/application.json','packaging/linux/install.sh','packaging/windows/version-info.txt',
                'packaging/windows/install-shortcut.ps1','.github/workflows/desktop.yml'}
    assert required <= selected
    assert not any('__MACOSX' in name or '.DS_Store' in name for name in selected)


def test_build_plan_does_not_claim_binary_completion():
    for target in ('macos-arm64','windows-amd64','linux-amd64'):
        recipe = plan(target, False)
        assert recipe['built'] is recipe['native_trading_qualified'] is False
        assert recipe['spec'] == 'packaging/oandafly.spec'


def test_cross_target_build_is_refused_before_dependency_work():
    target = 'windows-amd64' if native_target().key != 'windows-amd64' else 'macos-arm64'
    with pytest.raises(ValueError, match='Cross-compilation'):
        build(target)


def test_build_mutex_preserves_active_owner(tmp_path):
    lock = tmp_path/'.dev-build.lock'
    lock.mkdir()
    (lock/'owner.json').write_text('{"pid":123}')
    with pytest.raises(ValueError, match='Build lock'):
        with build_lock(tmp_path):
            pytest.fail('entered busy lock')
    assert (lock/'owner.json').read_text() == '{"pid":123}'


def test_build_mutex_released_on_failure(tmp_path):
    with pytest.raises(RuntimeError):
        with build_lock(tmp_path):
            assert (tmp_path/'.dev-build.lock/owner.json').is_file()
            raise RuntimeError('failure')
    assert not (tmp_path/'.dev-build.lock').exists()


def test_cleanup_mutex_blocks_build(tmp_path):
    (tmp_path/'.dev-environment.lock').mkdir()
    with pytest.raises(ValueError, match='Cleanup/setup'):
        with build_lock(tmp_path):
            pytest.fail('build entered cleanup')


def pe_bytes(subsystem, machine=0x8664):
    body = bytearray(256)
    body[:2] = b'MZ'
    struct.pack_into('<I',body,60,64)
    body[64:68] = b'PE\0\0'
    struct.pack_into('<H',body,68,machine)
    struct.pack_into('<H',body,88,0x20B)
    struct.pack_into('<H',body,156,subsystem)
    return body


def test_windows_pe_headers_are_parsed_without_windows(tmp_path):
    path = tmp_path/'app.exe'
    path.write_bytes(pe_bytes(2))
    assert binary_kind(path) == {'format':'PE','machine':0x8664,'subsystem':2}


def test_linux_elf_header_is_parsed_without_linux(tmp_path):
    body = bytearray(64); body[:6] = b'\x7fELF\x02\x01'; struct.pack_into('<H',body,18,62)
    path=tmp_path/'app'; path.write_bytes(body)
    assert binary_kind(path) == {'format':'ELF','machine':62}


def test_macos_arm64_header_is_parsed_without_macos(tmp_path):
    body=bytearray(64); body[:4]=b'\xcf\xfa\xed\xfe'; struct.pack_into('<I',body,4,0x0100000C)
    path=tmp_path/'app'; path.write_bytes(body)
    assert binary_kind(path) == {'format':'MACHO64','cpu_type':0x0100000C}


def test_invalid_executable_rejected(tmp_path):
    path=tmp_path/'app'; path.write_bytes(b'not binary')
    with pytest.raises(ValueError): binary_kind(path)


def test_windows_bundle_rejects_console_only_gui(tmp_path):
    bundle=tmp_path/'bundle'; bundle.mkdir()
    (bundle/'oandafly.exe').write_bytes(pe_bytes(3))
    (bundle/'oandafly-cli.exe').write_bytes(pe_bytes(3))
    with pytest.raises(ValueError, match='subsystems'):
        inspect_bundle(bundle,'windows-amd64')


def test_windows_bundle_requires_source_and_icons_not_only_exes(tmp_path):
    bundle=tmp_path/'bundle'; bundle.mkdir()
    (bundle/'oandafly.exe').write_bytes(pe_bytes(2))
    (bundle/'oandafly-cli.exe').write_bytes(pe_bytes(3))
    with pytest.raises(ValueError, match='parity'):
        inspect_bundle(bundle,'windows-amd64')


def test_app_and_portable_paths(tmp_path):
    app=tmp_path/'Oandafly.app'
    assert executable_path(app) == app/'Contents/MacOS/oandafly'
    assert bundle_paths(app,'macos-arm64')[1].name == 'oandafly-cli'


def test_clean_probe_environment_removes_secrets_and_injection(monkeypatch,tmp_path):
    for key in ('OANDA_TOKEN','PYTHONPATH','AWS_SECRET_ACCESS_KEY','MACOS_SIGN_IDENTITY','LD_PRELOAD','PYINSTALLER_RESET_ENVIRONMENT'):
        monkeypatch.setenv(key,'do-not-copy')
    env=clean_environment(tmp_path)
    assert not any(value=='do-not-copy' for value in env.values())
    assert env['HOME'] == str(tmp_path/'home')


def test_native_archive_never_overwrites_previous_release(tmp_path):
    target=tmp_path/'release.zip'; target.write_bytes(b'prior')
    with pytest.raises(ValueError,match='overwrite'):
        package_native(tmp_path/'bundle',target,'windows-amd64')
    assert target.read_bytes()==b'prior'


def test_windows_portable_archive_layout(tmp_path):
    bundle=tmp_path/'bundle'; bundle.mkdir(); (bundle/'oandafly.exe').write_bytes(b'fixture')
    output=tmp_path/'win.zip'; package_native(bundle,output,'windows-amd64')
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist()==['oandafly/oandafly.exe']


def test_lock_exports_include_desktop_build_dependency():
    text=(ROOT/'scripts/lock_dependencies.py').read_text()
    assert '"--extra", "desktop-build"' in text
    assert 'pyinstaller==6.22.3' in (ROOT/'requirements-build.in').read_text()


def test_gui_capability_does_not_remove_sqlite_or_containment_worker_gates():
    from oandafly.__main__ import doctor
    report=doctor()
    assert report['trading_qualified'] is False
    assert 'sqlite_3_51_3_or_newer' in report['checks']
    assert 'linux_production_containment' in report['checks']
    assert report['desktop']['window_tested'] is False


def test_missing_source_inventory_never_hashes_empty_tree(monkeypatch,tmp_path):
    import oandafly.neural.qualification as qualification
    from oandafly.domain.errors import SafetyFence
    monkeypatch.setattr(qualification,'__file__',str(tmp_path/'oandafly/neural/qualification.py'))
    with pytest.raises(SafetyFence,match='inventory'):
        qualification.source_identity()
