"""Complete source checks; these cannot qualify a frozen production artifact."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from oandafly.platforms import native_target
from scripts.project_integrity import create_manifest
from scripts.release_policy import document_digest, digest, environment, validate_test_evidence


def main() -> int:
    if len(sys.argv) != 1:
        raise ValueError('check takes no arguments')
    root = Path(__file__).resolve().parents[1]
    output = root / 'build/quality' / uuid4().hex
    output.mkdir(parents=True)
    env = environment(output / 'environment', native_target().key)
    binding = {'run_id': output.name, 'source_sha256': document_digest(create_manifest(root)),
               'contract_sha256': digest(root / 'packaging/release-tests.json'),
               'interpreter_sha256': digest(Path(sys.executable).resolve()), 'host': platform.platform()}
    (output / 'binding.json').write_text(json.dumps(binding, indent=2) + '\n')
    env.update(OANDAFLY_TEST_CONTRACT=str(root / 'packaging/release-tests.json'),
               OANDAFLY_TEST_BINDING=str(output / 'binding.json'), OANDAFLY_TEST_EVIDENCE=str(output / 'tests.json'))
    if os.name != 'nt':
        subprocess.run(['/bin/bash', '-n', str(root / 'dev.sh')], check=True, env=env)
    commands = [['scripts/project_integrity.py'], ['-m', 'ruff', 'check', 'src', 'tests', 'scripts'],
                ['scripts/run_release_tests.py']]
    for command in commands:
        subprocess.run([sys.executable, '-I', *command], check=True, cwd=root, env=env)
    if document_digest(create_manifest(root)) != binding['source_sha256']:
        raise ValueError('Source changed during checks')
    validate_test_evidence(output / 'tests.json', binding)
    print('Complete source check evidence: ' + str(output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
