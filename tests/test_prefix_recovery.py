import copy
from pathlib import Path
import sys
import unittest

try:
    import pytest
    import coverage
except ImportError:
    pytest = coverage = None


@unittest.skipIf(pytest is None, "pytest and coverage are needed for prefix recovery checks")
class PrefixRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        from retrace_prefix import GatedCoverage, PrefixCollector, validate_prefix
        cls.GatedCoverage = GatedCoverage
        cls.PrefixCollector = PrefixCollector
        cls.validate_prefix = staticmethod(validate_prefix)

    @staticmethod
    def observations():
        base = {'items': ['a', 'b', 'target', 'after'], 'source_commit': 'pin', 'cold_caches': True}
        metadata = {'full_collection': list(base['items']), 'executed_prefix': base['items'][:3],
                    'target': 'target', 'source_commit': 'pin', 'cold_caches': True,
                    'exit_code': 0, 'collection_errors': [],
                    'prefix_results': [{'id': identity, 'when': when, 'outcome': 'passed'}
                                       for identity in base['items'][:3] for when in ('setup', 'call', 'teardown')]}
        return base, metadata

    def test_prefix_requires_original_order_complete_phases_and_passed_target(self):
        base, metadata = self.observations()
        self.validate_prefix(base, metadata)
        for key, value in [('full_collection', ['a', 'target', 'b', 'after']),
                           ('executed_prefix', ['a', 'target']), ('cold_caches', False),
                           ('source_commit', 'other'), ('exit_code', 1), ('collection_errors', ['bad']),
                           ('prefix_results', metadata['prefix_results'][:-1])]:
            invalid = copy.deepcopy(metadata)
            invalid[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.validate_prefix(base, invalid)
        metadata['prefix_results'][3]['outcome'] = 'failed'
        with self.assertRaises(ValueError):
            self.validate_prefix(base, metadata)

    def test_loop_uses_original_nextitem_and_stops_after_target(self):
        from types import SimpleNamespace
        calls = []
        class Hook:
            def pytest_runtest_protocol(self, item, nextitem):
                calls.append((item.nodeid, nextitem.nodeid if nextitem else None))
        items = [SimpleNamespace(nodeid=name, ihook=Hook()) for name in ['a', 'target', 'after']]
        collector = object.__new__(self.PrefixCollector)
        collector.target = 'target'
        collector.executed_prefix = []
        session = SimpleNamespace(testsfailed=0, items=items, shouldfail=False, shouldstop=False)
        self.assertTrue(collector.pytest_runtestloop(session))
        self.assertEqual(calls, [('a', 'target'), ('target', 'after')])
        self.assertEqual(collector.executed_prefix, ['a', 'target'])

    def test_coverage_context_switches_while_paused_do_not_trace_prefix(self):
        class Actual:
            def __init__(self): self.events = []
            def start(self): self.events.append('start')
            def stop(self): self.events.append('stop')
            def switch_context(self, context): self.events.append(context)
        actual = Actual()
        gated = self.GatedCoverage(actual)
        gated.start()
        gated.stop()
        gated.switch_context('prefix-test')
        gated.switch_context('target')
        gated.start()
        gated.stop()
        self.assertEqual(actual.events, ['start', '@collection', 'stop', 'start', 'target', 'stop'])

    def test_source_verification_normalizes_line_endings_but_rejects_changed_content(self):
        import hashlib
        import tempfile
        from recover_map_trace import validate_prefix_sources
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = b'value = 1\r\n'
            (root/'module.py').write_bytes(raw)
            prefix = {'source_hashes': {'module.py': hashlib.sha256(raw).hexdigest()}}
            manifest = {'source_hashes': {'module.py': hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest()}}
            self.assertIn('Raw source bytes verified', validate_prefix_sources(prefix, manifest, root))
            (root/'module.py').write_bytes(b'value = 2\r\n')
            with self.assertRaises(ValueError):
                validate_prefix_sources(prefix, manifest, root)
