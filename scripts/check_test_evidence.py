#!/usr/bin/env python3
"""Validate exact collection evidence against the expected run/source binding."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.release_policy import validate_test_evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--binding', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = validate_test_evidence(args.report, json.loads(args.binding.read_text()))
        print(json.dumps({'passed': True, 'collected': len(result['collected']),
                          'executed': len(result['selected']), 'excluded': result['excluded'],
                          'native_trading_qualified': False}, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print('Invalid release evidence: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
