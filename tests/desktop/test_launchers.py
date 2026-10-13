"""Standalone source launcher/archive behavior; host-specific checks are labelled."""
from __future__ import annotations

import os
import shutil
import subprocess
import tarfile

import pytest

from scripts.build_desktop import package_native
from scripts.project_integrity import ROOT


def skeleton(tmp_path):
    (tmp_path/'src/oandafly').mkdir(parents=True)
    (tmp_path/'scripts').mkdir()
    (tmp_path/'src/oandafly/__init__.py').write_text('')
    (tmp_path/'scripts/dev.py').write_text('')
    (tmp_path/'pyproject.toml').write_text('[project]\nname="oandafly"\nversion="1.0.0"\n')
    shutil.copy2(ROOT/'dev.sh',tmp_path/'dev.sh')
    return tmp_path


@pytest.mark.posix_only
@pytest.mark.skipif(os.name=='nt',reason='Bash standalone cleanup is a POSIX launcher contract')
def test_shell_help_needs_no_python_or_lockfile(tmp_path):
    root=skeleton(tmp_path)
    result=subprocess.run(['/bin/bash',str(root/'dev.sh'),'help'],capture_output=True,text=True)
    assert result.returncode==0
    assert 'Windows AMD64' in result.stdout
    assert not (root/'uv.lock').exists()


@pytest.mark.posix_only
@pytest.mark.skipif(os.name=='nt',reason='Bash standalone cleanup is a POSIX launcher contract')
def test_clean_preserves_source_locks_and_external_symlink_target(tmp_path):
    root=skeleton(tmp_path/'source')
    (root/'uv.lock').write_text('preserved')
    (root/'src/oandafly/keep.py').write_text('pass')
    (root/'build').mkdir(); (root/'build/output').write_text('generated')
    outside=tmp_path/'outside'; outside.mkdir(); (outside/'keep').write_text('outside')
    (root/'dist').symlink_to(outside,target_is_directory=True)
    result=subprocess.run(['/bin/bash',str(root/'dev.sh'),'clean'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert (outside/'keep').read_text()=='outside'
    assert (root/'uv.lock').read_text()=='preserved'
    assert (root/'src/oandafly/keep.py').read_text()=='pass'
    assert not (root/'build').exists() and (root/'dist').is_symlink()


@pytest.mark.posix_only
@pytest.mark.skipif(os.name=='nt',reason='Bash standalone cleanup is a POSIX launcher contract')
def test_clean_refuses_active_build_without_deleting_it(tmp_path):
    root=skeleton(tmp_path)
    (root/'.dev-build.lock').mkdir()
    result=subprocess.run(['/bin/bash',str(root/'dev.sh'),'clean'],capture_output=True,text=True)
    assert result.returncode!=0
    assert (root/'.dev-build.lock').is_dir()


@pytest.mark.posix_only
@pytest.mark.skipif(os.name=='nt',reason='POSIX symlink-preserving portable tar contract')
def test_linux_archive_preserves_links_and_executable_mode(tmp_path):
    bundle=tmp_path/'bundle'; bundle.mkdir()
    exe=bundle/'oandafly'; exe.write_text('binary fixture'); exe.chmod(0o755)
    (bundle/'linked').symlink_to('oandafly')
    output=tmp_path/'linux.tar.gz'
    package_native(bundle,output,'linux-amd64')
    with tarfile.open(output) as archive:
        assert archive.getmember('oandafly/linked').issym()
        assert archive.getmember('oandafly/linked').linkname=='oandafly'
        assert archive.getmember('oandafly/oandafly').mode & 0o111
