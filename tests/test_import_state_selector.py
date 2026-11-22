import difflib
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from import_state_selector import ImportStateMixin
from selector import Selector


class Candidate(ImportStateMixin, Selector):
    pass


class Probe:
    def __init__(self, changed=(), error=False):
        self.changed, self.error = changed, error

    def compare(self, overrides):
        if self.error:
            raise RuntimeError('import failed')
        return self.changed, {'fields': {}}, {'fields': {}}


class Readers:
    def __init__(self, bits=0):
        self.bits = bits

    def readers(self, changed, before, after):
        return self.bits, {}


class ImportStateSelectionTests(unittest.TestCase):
    def candidate(self, source, *, collection=(), lines=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root/'sqlglot').mkdir()
        (root/'sqlglot/m.py').write_text(source)
        tests = [{'id': f'tests/test_m.py::test_{i}', 'number': i, 'seconds': 1} for i in range(3)]
        data = {'source_commit': 'fixture', 'source_hashes': {'sqlglot/m.py': hashlib.sha256(source.encode()).hexdigest()},
                'ids': [t['id'] for t in tests], 'collection_lines': {'sqlglot/m.py': list(collection)},
                'lines': {'sqlglot/m.py': lines or {}}, 'reads': {}, 'modules': {'sqlglot/m.py': '7'},
                'symbols': {}, 'attributes': {}}
        maps = root/'map.json.gz'
        maps.write_bytes(gzip.compress(json.dumps(data).encode()))
        candidate = Candidate(root, maps, tests)
        candidate._state_probe = Probe()
        candidate._state_readers = Readers()
        return candidate

    def patch(self, source, modified):
        return ''.join(difflib.unified_diff(source.splitlines(True), modified.splitlines(True),
                                           fromfile='a/sqlglot/m.py', tofile='b/sqlglot/m.py'))

    def test_unchanged_import_state_preserves_runtime_condition_consumers(self):
        source = 'def f(x):\n    if x == 1:\n        return 3\n    return 4\ny = f(0)\n'
        selector = self.candidate(source, collection=[1, 2, 4, 5], lines={'2': '3', '3': '1', '4': '2'})
        selected = selector.select(self.patch(source, source.replace('x == 1', 'x == 2')))
        self.assertEqual([t['number'] for t in selected], [0, 1])
        self.assertEqual(selector.state_last['reason'], 'refined')
        self.assertFalse(selector.import_needed)
        self.assertEqual(selector.map['collection_lines']['sqlglot/m.py'], [1, 2, 4, 5])

    def test_changed_module_value_selects_derived_field_reader(self):
        source = 'FLAG = True\n'
        selector = self.candidate(source, collection=[1])
        selector._state_probe = Probe(['sqlglot/m.py::FLAG', 'sqlglot/m.py:C:derived'])
        selector._state_readers = Readers(4)
        selected = selector.select(self.patch(source, 'FLAG = False\n'))
        self.assertEqual([t['number'] for t in selected], [2])

    def test_import_failure_keeps_existing_all_selection(self):
        source = 'FLAG = True\n'
        selector = self.candidate(source, collection=[1])
        selector._state_probe = Probe(error=True)
        selected = selector.select(self.patch(source, 'FLAG = False\n'))
        self.assertEqual([t['number'] for t in selected], [0, 1, 2])
        self.assertEqual(selector.state_last['reason'], 'snapshot_failed')

    def test_runtime_lambda_keeps_existing_call_precision(self):
        source = "TABLE = {'x': lambda x: x + 1}\n"
        selector = self.candidate(source, lines={'1': '1'})
        selector._state_probe = Mock()
        selector._state_probe.compare.side_effect = AssertionError('unnecessary snapshot')
        selected = selector.select(self.patch(source, source.replace('x + 1', 'x + 2')))
        self.assertEqual([t['number'] for t in selected], [0])
        self.assertEqual(selector.state_last['reason'], 'existing_lambda_call_readers')
