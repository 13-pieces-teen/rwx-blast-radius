"""V8 selection with baseline-only default and shared-executor dependencies.

No variant labels, expected outputs or mutation identifiers are used at runtime.
Shared-pool closure runs after precision refinements so they cannot remove it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from default_dependencies import DefaultsMixin
from import_state_selector import ImportStateMixin
from selector import Selector, schedule
from shared_executor_dependencies import SharedExecutorDependencies


class PrecisionSelector(ImportStateMixin, DefaultsMixin, Selector):
    def __init__(self, source, maps, tests, *, default_dependencies=None,
                 shared_executor_profile=None, precision_default_bindings=True,
                 import_state_profile=None, import_state_mode='readers'):
        super().__init__(source, maps, tests, precision_defaults=True,
                         default_dependencies=default_dependencies,
                         precision_default_bindings=precision_default_bindings,
                         import_state_profile=import_state_profile, import_state_mode=import_state_mode)
        self._shared_guard = None
        self._shared_guard_error = None
        try:
            self._shared_guard = SharedExecutorDependencies(self, shared_executor_profile)
        except Exception as error:
            self._shared_guard_error = type(error).__name__

    def select(self, patch):
        if self._shared_guard_error is not None:
            print('shared executor profile unavailable; selecting all tests', file=sys.stderr)
            return list(self.tests)
        selected = super().select(patch)
        try:
            return self._shared_guard.expand(patch, selected)
        except Exception as error:
            print(f'shared executor analysis fallback: {type(error).__name__}', file=sys.stderr)
            return list(self.tests)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path('/data'))
    parser.add_argument('--source', type=Path, default=Path('/opt/sqlglot'))
    parser.add_argument('--maps', type=Path,
                        default=Path(__file__).parent / 'artifacts/baseline-map.json.gz')
    parser.add_argument('--state-mode', choices=['off', 'equal', 'readers'], default='readers')
    args = parser.parse_args()
    tests = json.loads((args.data / 'tests.json').read_bytes())['tests']
    selector = PrecisionSelector(args.source, args.maps, tests, import_state_mode=args.state_mode)
    for path in sorted((args.data / 'commits').glob('*.patch')):
        selected = selector.select(path.read_text(encoding='utf-8'))
        workers = schedule(selected)
        print(json.dumps({'commit': path.stem, 'workers': workers}, separators=(',', ':')))
        print(f'{path.stem}: {len(selected)} tests, {len(workers)} workers', file=sys.stderr)
        if selector.state_last:
            print(json.dumps({'import_state': selector.state_last}, separators=(',', ':')), file=sys.stderr)


if __name__ == '__main__':
    main()
