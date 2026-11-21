import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.merge_class_setup_profile import merge


class ClassSetupProfileTests(unittest.TestCase):
    def test_union_retains_existing_edges_and_does_not_enable_new_fields(self):
        base = {"source_commit": "x", "ids": ["a", "b"], "source_hashes": {"s.py": "h"},
                "lines": {"s.py": {"1": "1"}}, "modules": {"s.py": "1"}, "reads": {},
                "symbols": {}, "attributes": {"s.py:C:FLAG": "1"}}
        profile = copy.deepcopy(base)
        profile.update(complete=True, failures=[], lines={"s.py": {"1": "2"}},
                       attributes={"s.py:C:FLAG": "2", "s.py:C:other": "2"},
                       class_dict_reads={"s.py:C": "2"}, lambda_call_readers={"s.py:1:0": "2"})
        readers = {"source_commit": "x", "ids": ["a", "b"], "complete": True,
                   "failures": [], "readers": {}, "collection": []}
        merged, dictionaries, lambdas = merge(base, profile, readers, readers)
        self.assertEqual(merged["lines"]["s.py"]["1"], "3")
        self.assertEqual(merged["attributes"], {"s.py:C:FLAG": "3"})
        self.assertEqual(dictionaries["readers"], {"s.py:C": "2"})
        self.assertEqual(lambdas["readers"], {"s.py:1:0": "2"})
        self.assertEqual(base["attributes"], {"s.py:C:FLAG": "1"})
        profile["ids"].reverse()
        with self.assertRaises(ValueError):
            merge(base, profile, readers, readers)

    @unittest.skipUnless(importlib.util.find_spec("pytest") and importlib.util.find_spec("coverage"),
                         "requires the mapping runtime's pytest and coverage")
    def test_real_pytest_fixture_setup_teardown_cleanup_use_class_context(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "sqlglot").mkdir()
            (source / "tests").mkdir()
            for name in ("setup", "teardown", "cleanup", "a", "b"):
                (source / "tests" / (name + ".txt")).write_text(name)
            (source / "tests/test_fixture.py").write_text('''
from pathlib import Path
import unittest
def read(name):
    return Path(__file__).with_name(name + '.txt').read_text()
class FixtureBase(unittest.TestCase):
    __test__ = False
    @classmethod
    def setUpClass(cls):
        assert cls.__name__ == 'TestFixture'
        read('setup')
        cls.addClassCleanup(read, 'cleanup')
    @classmethod
    def tearDownClass(cls):
        read('teardown')
class TestFixture(FixtureBase):
    __test__ = True
    def test_a(self):
        read('a')
    def test_b(self):
        read('b')
''')
            script = '''
import json, os, sys
from pathlib import Path
import pytest
from tools.build_maps import Collector
source = Path(sys.argv[1])
os.chdir(source)
c = Collector(source, source)
c.cov.start()
c.switch('@collection')
sys.addaudithook(c.audit)
code = pytest.main(['tests', '-q', '-p', 'no:cacheprovider'], plugins=[c])
c.cov.stop()
print('RESULT ' + json.dumps({'exit': code, 'ids': c.items, 'reads': {k: sorted(v) for k,v in c.reads.items()}}))
'''
            result = subprocess.run([sys.executable, "-c", script, directory],
                                    cwd=Path(__file__).resolve().parents[1], text=True,
                                    capture_output=True, check=True)
            data = json.loads(next(line[7:] for line in result.stdout.splitlines() if line.startswith("RESULT ")))
            self.assertEqual(data["exit"], 0, result.stdout + result.stderr)
            self.assertEqual(len(data["ids"]), 2)
            context = "@class:tests/test_fixture.py::TestFixture"
            for name in ("setup", "teardown", "cleanup"):
                self.assertEqual(data["reads"]["tests/" + name + ".txt"], [context])
            for name in ("a", "b"):
                self.assertEqual(data["reads"]["tests/" + name + ".txt"],
                                 ["tests/test_fixture.py::TestFixture::test_" + name])
