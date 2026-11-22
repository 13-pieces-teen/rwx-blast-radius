import ast
import copy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from default_dependencies import DefaultsMixin, bool_default_edit, valid_profile
from selector import Selector


class Candidate(DefaultsMixin, Selector):
    pass


class DefaultDependenciesTests(unittest.TestCase):
    def setUp(self):
        self.source = "def f(flag=True):\n    return flag\n"
        self.before = ast.parse(self.source)
        self.edit, _ = bool_default_edit(self.before, ast.parse(self.source.replace("True", "False")))
        self.base = {"source_commit": "fixture", "source_hashes": {"module.py": hashlib.sha256(self.source.encode()).hexdigest()},
                     "ids": ["tests/test_a.py::test_a", "tests/test_a.py::test_b"], "collection_lines": {},
                     "lines": {}, "reads": {}, "modules": {}, "symbols": {}, "attributes": {}}
        self.parameter = {"default": True, "kind": "positional", "seen": "1", "possible": "0", "opaque": "0",
                          "collection": False, "fixture": False, "collection_possible": False,
                          "collection_opaque": False, "fixture_possible": False, "fixture_opaque": False}
        self.record = {"parameters": {"flag": self.parameter}, "metadata_readers": "0", "metadata_collection": False,
                       "metadata_fixture": False, "identity_opaque": False, "state_effect": "closed"}
        self.profile = {"format_version": 1, "complete": True, "failures": [], "source_commit": "fixture",
                        "source_hashes": self.base["source_hashes"], "state_source_hashes": self.base["source_hashes"],
                        "ids": self.base["ids"], "modes": ["warm", "cold", "eager-import-collection"],
                        "metadata_scope": "source-tests-inspect-getattr", "state_guard_complete": True,
                        "identity_audit_complete": True, "identity_audit_modes": ["warm", "cold", "eager-import-collection"],
                        "persistent_writer_tests": "0", "persistent_opaque_tests": "0",
                        "functions": {"module.py:1:f": self.record}}

    def evaluate(self, body=1, protected=0):
        candidate = object.__new__(Candidate)
        candidate.map = self.base
        candidate.default_dependencies = self.profile
        candidate.precision_default_bindings = True
        return candidate._default_dependency_bits("module.py", self.before, self.edit, body, protected)

    def test_opaque_state_does_not_create_an_affected_call(self):
        self.record["state_effect"] = "opaque"
        self.profile["persistent_opaque_tests"] = "3"
        self.assertEqual(self.evaluate()[0], 0)

    def test_opposite_boolean_refinement_is_symmetric(self):
        self.before = ast.parse(self.source.replace("True", "False"))
        self.edit, _ = bool_default_edit(self.before, ast.parse(self.source))
        self.parameter["default"] = False
        self.assertEqual(self.evaluate()[0], 0)

    def test_new_observation_of_old_default_is_not_same_as_explicit_opposite(self):
        self.parameter.update(seen="3", possible="2")
        self.profile["persistent_writer_tests"] = "2"
        self.assertIsNone(self.evaluate()[0])

    def test_source_readers_can_propagate_changed_source(self):
        self.profile["persistent_writer_tests"] = "1"
        self.assertIsNone(self.evaluate(protected=1)[0])

    def test_new_observations_outside_old_body_are_not_intersected_away(self):
        self.parameter.update(seen="3", possible="2")
        self.assertEqual(self.evaluate()[0], 2)

    def test_missing_body_observation_is_retained(self):
        self.parameter["seen"] = "0"
        self.assertEqual(self.evaluate()[0], 1)

    def test_metadata_is_an_independent_dependency(self):
        self.record["metadata_readers"] = "2"
        self.assertEqual(self.evaluate()[0], 2)

    def test_unknown_function_identity_falls_back(self):
        self.record["identity_opaque"] = True
        self.assertIsNone(self.evaluate()[0])

    def test_possible_default_with_unknown_effect_falls_back(self):
        self.parameter["possible"] = "1"
        self.record["state_effect"] = "opaque"
        self.assertIsNone(self.evaluate()[0])

    def test_collection_opposite_is_distinct_from_default_use(self):
        self.base["collection_lines"] = {"module.py": [2]}
        self.parameter["collection"] = True
        self.assertEqual(self.evaluate()[0], 0)
        self.parameter["collection_possible"] = True
        self.assertIsNone(self.evaluate()[0])

    def test_old_collection_body_without_new_observation_falls_back(self):
        self.base["collection_lines"] = {"module.py": [2]}
        self.assertIsNone(self.evaluate()[0])

    def test_inline_definition_line_is_not_a_collection_body_call(self):
        self.before = ast.parse("def f(flag=True): return flag\n")
        self.edit, _ = bool_default_edit(self.before, ast.parse("def f(flag=False): return flag\n"))
        self.base["collection_lines"] = {"module.py": [1]}
        self.assertEqual(self.evaluate(body=0)[0], 0)

    def test_fixture_metadata_is_not_just_one_test(self):
        self.record["metadata_fixture"] = True
        self.assertIsNone(self.evaluate()[0])

    def test_strict_schema_requires_state_source_identity(self):
        self.assertTrue(valid_profile(self.profile, self.base))
        for field in ("state_source_hashes", "persistent_writer_tests", "persistent_opaque_tests"):
            changed = copy.deepcopy(self.profile)
            del changed[field]
            self.assertFalse(valid_profile(changed, self.base))

    def test_strict_schema_requires_metadata_and_phase_fields(self):
        for field in ("metadata_readers", "state_effect"):
            changed = copy.deepcopy(self.profile)
            del changed["functions"]["module.py:1:f"][field]
            self.assertFalse(valid_profile(changed, self.base))
        for field in ("seen", "opaque", "collection_possible", "fixture_opaque"):
            changed = copy.deepcopy(self.profile)
            del changed["functions"]["module.py:1:f"]["parameters"]["flag"][field]
            self.assertFalse(valid_profile(changed, self.base))

    def test_bitsets_cannot_reference_other_tests_or_unseen_calls(self):
        self.profile["persistent_writer_tests"] = "4"
        self.assertFalse(valid_profile(self.profile, self.base))
        self.profile["persistent_writer_tests"] = "0"
        self.parameter.update(seen="0", possible="1")
        self.assertFalse(valid_profile(self.profile, self.base))

    def test_truncated_optional_gzip_falls_back_without_crashing(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "module.py").write_text(self.source, encoding="utf-8")
            (directory / "map.gz").write_bytes(gzip.compress(json.dumps(self.base).encode()))
            (directory / "default-dependencies.json.gz").write_bytes(b"\x1f\x8b\x08")
            tests = [{"id": name, "number": index, "seconds": .1} for index, name in enumerate(self.base["ids"])]
            candidate = Candidate(directory, directory / "map.gz", tests, precision_defaults=True)
            self.assertIsNone(candidate.default_dependencies)

    def test_body_and_signature_changes_are_not_default_only_edits(self):
        mutations = ["def f(flag=False):\n    return not flag\n", "def f(other=False):\n    return other\n",
                     "async def f(flag=False):\n    return flag\n", "def f(flag=0):\n    return flag\n"]
        for changed in mutations:
            self.assertIsNone(bool_default_edit(self.before, ast.parse(changed))[0])


if __name__ == "__main__":
    unittest.main()
