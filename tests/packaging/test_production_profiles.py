"""Synthetic orchestration fixtures; these never certify native Apple operations."""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from oandafly.macos import profile, version
from scripts import macos_profile, macos_signing
from scripts.baseline_acceptance import REQUIRED_CHECKS, validate as validate_baseline
from scripts.build_desktop import ROOT, build, build_lock
from scripts.macho_audit import image
from scripts.release_artifacts import payload_identity, publish
from scripts.release_policy import COMMON_GATES, PROFILES, document_digest, environment, policy, validate_test_evidence


def observed():
    p = profile()
    return {**{key: p[key] for key in ('xcode_version', 'clang_product', 'clang_version', 'clang_build', 'sdk_version', 'sdk_build')},
            'architecture': 'arm64', 'translated': '0', 'hardware': 'Apple M4',
            'host_version': '26.6.2', 'host_build': '25G83', 'xcode_build': '27A266a',
            'installed_xcode_minimum_host': '26.6',
            'system_developer_dir': '/Applications/Xcode.app/Contents/Developer',
            'developer_dir': '/Applications/Xcode.app/Contents/Developer',
            'clang_path': '/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang',
            'sdk_path': '/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk'}


def test_requested_profile_accepts_new_sdk_with_older_runtime():
    assert macos_profile.validate(observed(), {}) == []
    assert profile()['application_minimum'] == profile()['deployment_target'] == '26.0'
    equivalent = observed() | {'xcode_version': '27.0.0', 'sdk_version': '27'}
    assert macos_profile.validate(equivalent, {}) == []


@pytest.mark.parametrize(('field', 'wrong'), [
    ('architecture', 'x86_64'), ('translated', '1'), ('host_version', '26.0'),
    ('clang_product', 'clang'), ('clang_version', '21.0.1'), ('clang_build', 'clang-2100.1.1'),
    ('xcode_version', '26.6'), ('xcode_build', ''), ('sdk_version', '26.0'),
    ('sdk_build', '27A266a'), ('sdk_build', '25G83'), ('sdk_build', ''),
    ('developer_dir', '/Library/Developer/CommandLineTools'),
    ('clang_path', '/opt/homebrew/bin/clang'), ('sdk_path', '/Other/Xcode/SDKs/MacOSX.sdk'),
    ('installed_xcode_minimum_host', None), ('installed_xcode_minimum_host', '27.0')])
def test_independent_toolchain_mismatches_rejected(field, wrong):
    assert macos_profile.validate(observed() | {field: wrong}, {})


@pytest.mark.parametrize('env', [
    {'MACOSX_DEPLOYMENT_TARGET': '27.0'}, {'MACOSX_DEPLOYMENT_TARGET': '25.9'},
    {'MACOSX_DEPLOYMENT_TARGET': 'bad'}, {'DEVELOPER_DIR': 'relative'},
    {'DEVELOPER_DIR': '/Other/Xcode.app/Contents/Developer'}, {'TOOLCHAINS': 'org.llvm.21'},
    {'SDKROOT': '/Other/MacOSX.sdk'}])
def test_conflicting_inherited_build_settings_rejected(env):
    assert macos_profile.validate(observed(), env)


@pytest.mark.parametrize(('os_version', 'accepted'), [('9.9', False), ('25.9', False), ('26', True), ('26.0', True), ('26.1', True), ('27.0', True)])
def test_runtime_numeric_boundaries(monkeypatch, os_version, accepted):
    from oandafly import macos
    monkeypatch.setattr(macos.sys, 'platform', 'darwin')
    monkeypatch.setattr(macos.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(macos.platform, 'mac_ver', lambda: (os_version, (), ''))
    if accepted:
        macos.require_runtime()
    else:
        with pytest.raises(ValueError, match='requires macOS'): macos.require_runtime()


@pytest.mark.parametrize('value', ['', '26beta', '26.-1', '26.0.0.0', ' 26.0'])
def test_invalid_version_not_coerced(value):
    with pytest.raises(ValueError): version(value)


def fake_checkout(tmp_path):
    root = tmp_path / 'source with spaces ü'
    (root / 'src/oandafly').mkdir(parents=True)
    (root / 'scripts').mkdir()
    (root / '.venv/bin').mkdir(parents=True)
    (root / 'pyproject.toml').write_text('[project]\nname="oandafly"\nversion="1.0.0"\n')
    (root / 'src/oandafly/__init__.py').write_text('')
    # This test-only executable models the backend argument/environment boundary.
    exe = root / '.venv/bin/python'
    exe.write_text('#!' + sys.executable + '\nimport json,os,sys\nprint(json.dumps({"argv":sys.argv[1:],"identity":os.environ.get("MACOS_SIGN_IDENTITY"),"profile":os.environ.get("MACOS_NOTARY_PROFILE")}))\n')
    exe.chmod(0o755)
    (root / 'scripts/dev.py').write_text('')
    shutil.copy2(ROOT / 'dev.sh', root / 'dev.sh')
    return root


@pytest.mark.posix_only
@pytest.mark.skipif(os.name == 'nt', reason='Bash contract')
@pytest.mark.parametrize('command', ['build', 'build-unsigned', 'release'])
@pytest.mark.parametrize(('identity', 'notary'), [(None, None), ('', ''), ('override with spaces', None),
    (None, 'profile with spaces'), ('', 'profile with spaces'), ('override', ''), ('custom', 'custom profile'),
    ('$(touch should-not-exist)', '`touch should-not-exist`')])
def test_real_bash_default_and_override_semantics(tmp_path, command, identity, notary):
    root = fake_checkout(tmp_path)
    env = {k:v for k,v in os.environ.items() if k not in {'MACOS_SIGN_IDENTITY','MACOS_NOTARY_PROFILE'}}
    if identity is not None: env['MACOS_SIGN_IDENTITY'] = identity
    if notary is not None: env['MACOS_NOTARY_PROFILE'] = notary
    result = subprocess.run(['/bin/bash', str(root / 'dev.sh'), command], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value['identity'] == (identity or '351A9115F928304981211FEB5866E94B6C760AD1')
    assert value['profile'] == (notary or 'macos-notary')
    assert value['argv'][-1] == command
    assert not (root / 'should-not-exist').exists()
    assert not (tmp_path / 'should-not-exist').exists()


@pytest.mark.posix_only
@pytest.mark.skipif(os.name == 'nt', reason='Bash contract')
@pytest.mark.parametrize('command', ['build', 'build-unsigned', 'release'])
@pytest.mark.parametrize(('argument', 'code'), [('--help', 0), ('--identity', 1), ('--skip-tests', 1), ('--target', 1), ('--plan', 1)])
def test_build_parsing_precedes_interpreter_and_side_effects(tmp_path, command, argument, code):
    root = fake_checkout(tmp_path)
    (root / '.venv/bin/python').unlink()
    result = subprocess.run(['/bin/bash', str(root / 'dev.sh'), command, argument], env={'PATH':'/usr/bin:/bin'}, capture_output=True, text=True)
    assert result.returncode == code, result.stderr
    assert not (root / '.dev-environment.lock').exists()
    assert not (root / 'build').exists()


def test_production_profiles_share_all_quality_gates():
    unsigned, signed = [policy(p, 'macos-arm64') for p in PROFILES]
    assert unsigned['common_gates'] == signed['common_gates'] == list(COMMON_GATES)
    assert set(unsigned['trust_gates'].values()) == {'not_applicable_unsigned_profile'}
    assert set(signed['trust_gates'].values()) == {'pending'}
    assert not unsigned['production_quality_ready'] and not signed['production_quality_ready']


@pytest.mark.parametrize('profile_name', PROFILES)
def test_shared_failure_and_cancellation_never_publish(monkeypatch, tmp_path, profile_name):
    import scripts.build_desktop as module
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'identity', lambda: {})
    monkeypatch.setattr(module, 'build_lock', lambda: build_lock(tmp_path))
    monkeypatch.setattr(module, 'preflight', lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    (tmp_path / 'dist').mkdir(); (tmp_path / 'dist/prior').write_text('last good')
    with pytest.raises(KeyboardInterrupt): build(module.native_target().key, profile=profile_name)
    assert (tmp_path / 'dist/prior').read_text() == 'last good'
    receipt = json.loads(next((tmp_path / 'build').rglob('failed.json')).read_text())
    assert not receipt['passed'] and receipt['failed_stage'] == 'preflight'
    assert not (tmp_path / '.dev-build.lock').exists()


def test_unsigned_preflight_never_probes_signing(monkeypatch, tmp_path):
    import scripts.build_desktop as module
    monkeypatch.setenv('MACOS_SIGN_IDENTITY', 'unavailable')
    monkeypatch.setenv('MACOS_NOTARY_PROFILE', 'unavailable')
    for key in list(os.environ):
        if key.startswith('PYTEST_'): monkeypatch.delenv(key)
    monkeypatch.setattr(module, 'environment', lambda *a: {})
    monkeypatch.setattr(macos_profile, 'discover', lambda: {'qualified': True, 'issues': [], 'observed': observed()})
    monkeypatch.setattr(macos_signing, 'preflight', lambda *a: pytest.fail('unsigned accessed signing credentials'))
    (tmp_path / 'logs').mkdir()
    with pytest.raises(ValueError, match='baseline|containment|App Sandbox'):
        module.preflight('macos-arm64', 'production_unsigned', tmp_path)


def test_signed_backend_preserves_environment_values_without_defaults():
    env = {'MACOS_SIGN_IDENTITY':'Developer ID Application: A Person (TEAM)', 'MACOS_NOTARY_PROFILE':'Profile With Spaces'}
    assert macos_signing.configuration(env) == tuple(env.values())
    with pytest.raises(ValueError, match='exported'): macos_signing.configuration({})


@pytest.mark.parametrize('bad', ['', '-', '-option', '\nkey', '   '])
def test_signed_invalid_settings_fail_truthfully(bad):
    with pytest.raises(ValueError):
        macos_signing.configuration({'MACOS_SIGN_IDENTITY':bad, 'MACOS_NOTARY_PROFILE':'test'})


def test_child_environment_excludes_signing_broker_and_injection(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith('PYTEST_'): monkeypatch.delenv(key)
    for key in ('MACOS_SIGN_IDENTITY','MACOS_NOTARY_PROFILE','OANDA_TOKEN','DYLD_INSERT_LIBRARIES','PYTHONPATH'):
        monkeypatch.setenv(key, 'SYNTHETIC_SECRET')
    env = environment(tmp_path, 'macos-arm64')
    assert 'SYNTHETIC_SECRET' not in env.values()
    monkeypatch.setenv('PYTEST_ADDOPTS', '-k subset')
    with pytest.raises(ValueError, match='pytest'): environment(tmp_path, 'macos-arm64')


def macho(path, minimum=26, sdk=27, cpu=0x0100000C):
    path.write_bytes(struct.pack('<8I',0xFEEDFACF,cpu,0,2,1,24,0,0)+struct.pack('<6I',0x32,24,1,minimum<<16,sdk<<16,0))
    return path


def test_macho_distinguishes_sdk_from_deployment_target(tmp_path):
    result = image(macho(tmp_path / 'binary'))
    assert result['minimum_os'] == ['26.0.0'] and result['sdk_stamps'] == ['27.0.0']


@pytest.mark.parametrize(('minimum','cpu'), [(27,0x0100000C),(26,0x01000007)])
def test_incompatible_nested_image_rejected(tmp_path, minimum, cpu):
    with pytest.raises(ValueError): image(macho(tmp_path/'extension.so', minimum=minimum, cpu=cpu))


@pytest.mark.posix_only
@pytest.mark.skipif(os.name == 'nt', reason='POSIX modes and symlinks')
def test_payload_identity_binds_modes_and_rejects_escape(tmp_path):
    (tmp_path/'app').mkdir(); p=tmp_path/'app/exe'; p.write_bytes(b'fixture'); p.chmod(0o755)
    before=payload_identity(tmp_path/'app'); p.chmod(0o644)
    assert payload_identity(tmp_path/'app') != before
    (tmp_path/'app/link').symlink_to('/tmp')
    with pytest.raises(ValueError, match='link'): payload_identity(tmp_path/'app')


def test_atomic_publication_refuses_incomplete_sidecars(tmp_path):
    stage=tmp_path/'staging'; stage.mkdir()
    (stage/'build.json').write_text(json.dumps({'passed':True,'production_quality_ready':True,'sha256':'0'*64,'archive_name':'missing.zip'}))
    prior=tmp_path/'dist/prior'; prior.mkdir(parents=True); (prior/'keep').write_text('last good')
    with pytest.raises((ValueError,FileNotFoundError)): publish(stage,tmp_path/'dist/new')
    assert (prior/'keep').read_text()=='last good' and not (tmp_path/'dist/new').exists()


def evidence(binding):
    return {'format':'oandafly.release-test-evidence.1','binding':binding,'passed':True,'exitstatus':0,
            'issues':[],'collected':['test_a','test_linux'],'selected':['test_a'],'excluded':{'test_linux':['linux_containment']},
            'results':{'test_a':{'setup':'passed','call':'passed','teardown':'passed'}}}


@pytest.mark.parametrize('damage', ['stale','duplicate','skip','missing','failed'])
def test_release_evidence_rejects_incomplete_outcomes(tmp_path,damage):
    binding={'run_id':'fresh'}; value=evidence(binding)
    if damage=='stale': value['binding']={'run_id':'old'}
    if damage=='duplicate': value['collected'].append('test_a')
    if damage=='skip': value['results']['test_a']['call']='skipped'
    if damage=='missing': value['results']={}
    if damage=='failed': value['passed']=False
    p=tmp_path/'evidence.json'; p.write_text(json.dumps(value))
    with pytest.raises(ValueError): validate_test_evidence(p,binding)


def signed_baseline(request, **changes):
    key=Ed25519PrivateKey.generate()
    result={'request_sha256':document_digest(request),'completed_at':time.time(),'runtime_os':'26.0',
            'architecture':'arm64','interactive_session':True,'host_build':'test-only','hardware':'test-only',
            'virtualized':True,'payload_sha256':request['payload_sha256'],'checks':{k:'passed' for k in REQUIRED_CHECKS},**changes}
    raw=json.dumps(result,sort_keys=True,separators=(',',':')).encode()
    return {'result':result,'signature':key.sign(raw).hex()},key.public_key().public_bytes_raw().hex()


@pytest.mark.parametrize('damage',['os','payload','profile','signature','missing'])
def test_baseline_rejects_other_runtime_payload_or_untrusted_receipt(damage):
    request={'created_at':time.time(),'profile':'production_unsigned','payload_sha256':'a'*64,'nonce':'test-only'}
    changes={}
    if damage=='os': changes['runtime_os']='26.1'
    if damage=='payload': changes['payload_sha256']='b'*64
    if damage=='missing': changes['checks']={}
    receipt,key=signed_baseline(request,**changes)
    if damage=='signature': receipt['signature']='0'*128
    if damage=='profile': request['profile']='production_signed'
    with pytest.raises(ValueError): validate_baseline(request,receipt,key)


def test_baseline_accepts_only_complete_bound_signed_runner_evidence():
    request={'created_at':time.time(),'profile':'production_unsigned','payload_sha256':'a'*64,'nonce':'test-only'}
    receipt,key=signed_baseline(request)
    assert validate_baseline(request,receipt,key)['runtime_os']=='26.0'


def test_source_snapshot_rejects_unreviewed_change(monkeypatch,tmp_path):
    import scripts.build_desktop as module
    monkeypatch.setattr(module,'ROOT',tmp_path)
    monkeypatch.setattr(module,'audit',lambda *a,**k:{'source_check_passed':False,'issues':['Changed manifested file: module.py']})
    with pytest.raises(ValueError,match='manifest'):
        module.snapshot(tmp_path/'snapshot')
    assert not (tmp_path/'snapshot').exists()


def test_cross_profile_concurrent_build_lock_cannot_be_stolen(tmp_path):
    with build_lock(tmp_path):
        before=(tmp_path/'.dev-build.lock/owner.json').read_text()
        with pytest.raises(ValueError,match='lock'):
            with build_lock(tmp_path): pytest.fail('second profile entered')
        assert (tmp_path/'.dev-build.lock/owner.json').read_text()==before
    assert not (tmp_path/'.dev-build.lock').exists()


@pytest.mark.parametrize('failure',['missing','skipped','advisory','duplicate'])
def test_audit_coverage_gaps_fail(tmp_path,failure):
    from scripts.release_policy import validate_audit
    lock=tmp_path/'requirements.lock';lock.write_text('example==1.0 --hash=sha256:'+'a'*64+'\n')
    data={'name':'example','version':'1.0','vulns':[]}
    result={'dependencies':[data]}
    if failure=='missing': result['dependencies']=[]
    if failure=='skipped': data['skip_reason']='unavailable'
    if failure=='advisory': data['vulns']=[{'id':'TEST-ONLY'}]
    if failure=='duplicate': result['dependencies'].append(dict(data))
    with pytest.raises(ValueError,match='audit'): validate_audit(lock,result)


def test_audit_checks_exact_applicable_marker_pins(tmp_path):
    from scripts.release_policy import validate_audit
    lock=tmp_path/'requirements.lock'
    lock.write_text('example==1.0 --hash=sha256:'+'a'*64+'\nother==2.0; python_version < "3.0" --hash=sha256:'+'b'*64+'\n')
    assert validate_audit(lock,{'dependencies':[{'name':'example','version':'1.0','vulns':[]}]})['packages']==1


def test_unsigned_receipt_cannot_claim_apple_trust(tmp_path):
    from scripts.verify_build import verify_release
    (tmp_path/'build.json').write_text(json.dumps({'profile':'production_unsigned','passed':False}))
    with pytest.raises(ValueError,match='Incomplete'): verify_release(tmp_path,logs=tmp_path/'logs')


@pytest.mark.posix_only
@pytest.mark.skipif(os.name=='nt',reason='Bash cleanup')
def test_clean_preserves_every_published_byte(tmp_path):
    root=fake_checkout(tmp_path)
    published=root/'dist/production-unsigned/old/evidence/__pycache__'
    published.mkdir(parents=True); (published/'preserve.pyc').write_bytes(b'published fixture')
    result=subprocess.run(['/bin/bash',str(root/'dev.sh'),'clean'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert (published/'preserve.pyc').read_bytes()==b'published fixture'


@pytest.mark.parametrize('mode',['signature','notary','timeout','gatekeeper'])
def test_signed_trust_failure_never_falls_back(monkeypatch,tmp_path,mode):
    bundle=tmp_path/'Oandafly.app';bundle.mkdir()
    logs=tmp_path/'logs';logs.mkdir()
    calls=[]
    def fake_run(argv,log,env,timeout=60):
        calls.append(argv)
        if (mode=='signature' and argv[0].endswith('codesign')) or (mode=='gatekeeper' and argv[0].endswith('spctl')):
            raise ValueError('synthetic Apple failure')
        if 'submit' in argv: return json.dumps({'id':'01234567-0123-0123-0123-0123456789ab'})
        if 'wait' in argv:
            if mode=='timeout': raise ValueError('synthetic timeout')
            return json.dumps({'status':'Invalid' if mode=='notary' else 'Accepted'})
        return ''
    monkeypatch.setattr(macos_signing,'run',fake_run)
    monkeypatch.setattr(macos_signing,'verify_signature',lambda *a,**k:{'verified':True})
    env={'MACOS_SIGN_IDENTITY':'test identity','MACOS_NOTARY_PROFILE':'test profile'}
    with pytest.raises(ValueError): macos_signing.sign_and_notarize(bundle,{'images':{}},env,logs,'TESTTEAM')
    assert not any('--sign' in cmd and cmd[cmd.index('--sign')+1]=='-' for cmd in calls)
    if mode in ('notary','timeout','gatekeeper'):
        assert (logs/'notary-submission-id.txt').read_text().strip()=='01234567-0123-0123-0123-0123456789ab'
        assert sum('submit' in cmd for cmd in calls)==1


@pytest.mark.parametrize('profile_name',PROFILES)
@pytest.mark.parametrize(('failing_log','stage'),[('locks.txt','locked_environment'),('dependencies.txt','locked_environment'),('ruff.txt','quality'),('tests.txt','complete_tests')])
def test_common_pipeline_failures_preserve_prior_release(monkeypatch,tmp_path,profile_name,failing_log,stage):
    import scripts.build_desktop as module
    monkeypatch.setattr(module,'ROOT',tmp_path)
    monkeypatch.setattr(module,'identity',lambda:{})
    monkeypatch.setattr(module,'build_lock',lambda:build_lock(tmp_path))
    monkeypatch.setattr(module,'preflight',lambda *a:({},{}))
    def fixture_snapshot(destination):
        (destination/'packaging').mkdir(parents=True)
        (destination/'packaging/release-tests.json').write_text('{}')
        for name in module.RELEASE_FILES: (destination/name).write_text('TEST_ONLY')
        return {}
    monkeypatch.setattr(module,'snapshot',fixture_snapshot)
    def fake_logged(command,log,**kwargs):
        if log.name==failing_log: raise ValueError('synthetic gate failure')
        if log.name=='venv.txt':
            path=log.parent.parent/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
            path.parent.mkdir(parents=True);path.write_bytes(b'TEST_ONLY_INTERPRETER')
        log.write_text('TEST_ONLY_SUCCESS')
    monkeypatch.setattr(module,'run_logged',fake_logged)
    (tmp_path/'dist').mkdir();(tmp_path/'dist/prior').write_text('last good')
    with pytest.raises(ValueError,match='synthetic gate failure'): module.build(module.native_target().key,profile=profile_name)
    receipt=json.loads(next((tmp_path/'build').rglob('failed.json')).read_text())
    assert receipt['failed_stage']==stage and receipt['gates'][stage]=='failed'
    assert not receipt['production_quality_ready']
    assert (tmp_path/'dist/prior').read_text()=='last good'
    assert not (tmp_path/'.dev-build.lock').exists()


def test_source_cannot_impersonate_embedded_runtime():
    from oandafly.runtime_probe import probe
    with pytest.raises(ValueError,match='frozen'): probe()


def test_embedded_runtime_exercises_are_credential_free_in_source_smoke(monkeypatch):
    # The probe exercises optional extras; the unlocked `.[dev]` source job lacks them.
    keyring = pytest.importorskip('keyring')
    pytest.importorskip('pyarrow')
    from oandafly.runtime_probe import probe
    monkeypatch.setattr(keyring,'get_password',lambda *a:pytest.fail('probe requested credentials'))
    report=probe(require_frozen=False)
    assert report['passed'] and report['frozen'] is False
    assert report['credentials_accessed'] is False and report['native_containment_qualified'] is False
