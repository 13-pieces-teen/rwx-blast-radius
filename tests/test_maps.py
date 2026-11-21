import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.export_maps import union_maps, validate_observations
from tools.recover_map_trace import replace_retried_reads


def sample_map(bit):
    return {
        "source_commit": "pinned", "ids": ["warm", "cold"], "source_hashes": {"m.py": "hash"},
        "lines": {"m.py": {"1": bit}}, "modules": {"m.py": bit},
        "reads": {"fixture.sql": bit}, "symbols": {"m.py:C": bit},
        "attributes": {"m.py:C:FLAG": bit}, "collection_lines": {"m.py": [1]},
        "collection_attributes": [], "collection_reads": [],
    }


class UnionMapsTests(unittest.TestCase):
    def test_recovery_replaces_diagnostic_reads_but_keeps_other_test_contexts(self):
        original = {"scope.py": ["failed"], "shared.py": ["failed", "other"],
                    "fixture.sql": ["failed", "@class:Fixture"],
                    "import.py": ["@collection"]}
        retried = {"fixture.sql": ["failed"], "new.sql": ["failed"]}
        merged, removed = replace_retried_reads(original, retried, {"failed"})
        self.assertNotIn("scope.py", merged)
        self.assertEqual(merged["shared.py"], ["other"])
        self.assertEqual(merged["fixture.sql"], ["@class:Fixture", "failed"])
        self.assertEqual(merged["import.py"], ["@collection"])
        self.assertEqual(merged["new.sql"], ["failed"])
        self.assertEqual(removed, [{"path": "scope.py", "context": "failed"},
                                   {"path": "shared.py", "context": "failed"}])
        self.assertEqual(original["scope.py"], ["failed"])

    def test_direct_test_metadata_reads_are_recorded_without_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for package in ("sqlglot", "tests"):
                (source / package).mkdir()
                (source / package / "__init__.py").write_text("")
            (source / "sqlglot/subject.py").write_text(
                "class Subject:\n    FLAG = True\n    arg_types = {'this': True}\n"
                "class Derived(Subject):\n    pass\n")
            (source / "tests/test_subject.py").write_text(
                "from sqlglot.subject import Subject, Derived\n"
                "def observe():\n    return Subject.FLAG, Subject.arg_types, Subject.__dict__, Derived.__dict__, vars(Derived)\n")
            script = '''
from collections import defaultdict
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from tools.attribute_reads import install
c = SimpleNamespace(source=Path(sys.argv[1]), context='@collection',
                    attributes=defaultdict(set), symbols=defaultdict(set), modules=defaultdict(set),
                    class_dict_reads=defaultdict(set))
install(c)
sys.path.insert(0, sys.argv[1])
from tests.test_subject import observe
c.context = 'direct-test'
observe()
print(json.dumps({category: {k: sorted(v) for k, v in getattr(c, category).items()}
                  for category in ('attributes', 'symbols', 'class_dict_reads')}))
'''
            result = subprocess.run([sys.executable, "-c", script, directory],
                                    cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, check=True)
            observed = json.loads(result.stdout)
            for field in ("FLAG", "arg_types"):
                self.assertIn("direct-test", observed["attributes"]["sqlglot/subject.py:Subject:" + field])
            self.assertIn("direct-test", observed["symbols"]["sqlglot/subject.py:Subject"])
            self.assertIn("direct-test", observed["class_dict_reads"]["sqlglot/subject.py:Derived"])

    def test_export_rejects_partial_failed_or_empty_baselines(self):
        completed = {"items": ["a", "b"], "results": [
            {"id": "a", "when": "setup", "outcome": "passed"},
            {"id": "a", "when": "call", "outcome": "passed"},
            {"id": "a", "when": "teardown", "outcome": "passed"},
            {"id": "b", "when": "setup", "outcome": "skipped"},
            {"id": "b", "when": "teardown", "outcome": "passed"}]}
        validate_observations(completed)
        for broken in ({"items": ["a", "b"], "results": completed["results"][:1]},
                       {"items": [], "results": []},
                       {"items": ["a", "b"], "results": completed["results"][:-1]},
                       {"items": ["a", "b"], "results": completed["results"] + [
                           {"id": "a", "when": "teardown", "outcome": "failed"}]}):
            with self.assertRaises(ValueError):
                validate_observations(broken)

    def test_preserves_warm_and_cold_edges_without_enabling_new_narrowing(self):
        cold, warm = sample_map("2"), sample_map("1")
        cold["attributes"]["m.py:C:arg_types"] = "2"
        warm["lines"]["m.py"]["9"] = "1"
        warm["collection_lines"]["m.py"].append(9)
        warm["collection_attributes"] = ["m.py:C:FLAG"]
        cold["collection_reads"] = ["fixture.sql"]
        union_maps(cold, warm, attribute_keys=set(warm["attributes"]))
        for category, key in (("modules", "m.py"), ("symbols", "m.py:C"),
                              ("reads", "fixture.sql"), ("attributes", "m.py:C:FLAG")):
            self.assertEqual(cold[category][key], "3")
        self.assertEqual(cold["lines"]["m.py"], {"1": "3", "9": "1"})
        self.assertNotIn("m.py:C:arg_types", cold["attributes"])
        self.assertEqual(cold["collection_lines"]["m.py"], [1, 9])
        self.assertEqual(cold["collection_attributes"], ["m.py:C:FLAG"])
        self.assertEqual(cold["collection_reads"], ["fixture.sql"])

    def test_rejects_mismatched_test_order_or_source(self):
        original = sample_map("1")
        for key, replacement in (("ids", ["cold", "warm"]), ("source_commit", "other"),
                                 ("source_hashes", {"m.py": "other"})):
            other = copy.deepcopy(original)
            other[key] = replacement
            with self.subTest(key=key), self.assertRaises(ValueError):
                union_maps(copy.deepcopy(original), other)
