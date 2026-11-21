import ast
import difflib
import gzip
import json
from pathlib import Path
import random
import tempfile
import unittest

from selector import (FileChange, Selector, apply_change, parse_patch, schedule,
                      identity_edits, identity_equivalent_types)


def diff(old, new, path="module.py"):
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                      fromfile="a/" + path, tofile="b/" + path))


class PatchTests(unittest.TestCase):
    def test_randomized_roundtrips(self):
        rng = random.Random(17)
        for _ in range(200):
            before = [f"line_{j}\n" for j in range(rng.randrange(1, 50))]
            after = before[:]
            for _ in range(rng.randrange(1, 5)):
                pos = rng.randrange(len(after) + 1)
                after[pos:pos + rng.randrange(0, 3)] = [
                    f"new_{rng.randrange(10000)}\n" for _ in range(rng.randrange(0, 4))]
            old, new = "".join(before), "".join(after)
            if old != new:
                result, _, _ = apply_change(old, parse_patch(diff(old, new))[0])
                self.assertEqual(result, new)

    def test_rejects_bad_context_and_paths(self):
        change = parse_patch(diff("x = 1\n", "x = 2\n"))[0]
        with self.assertRaises(ValueError):
            apply_change("x = 3\n", change)
        with self.assertRaises(ValueError):
            parse_patch(diff("x\n", "y\n", "../escape.py"))

    def test_no_final_newline(self):
        patch = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n\\ No newline at end of file\n+x = 2\n\\ No newline at end of file\n"
        actual, _, _ = apply_change("x = 1", parse_patch(patch)[0])
        self.assertEqual(actual, "x = 2")


class ScheduleTests(unittest.TestCase):
    def test_overhead_and_empty_selection(self):
        for durations, expected in [([], 0), ([1], 1), ([.1, .1], 1), ([10, 10], 2), ([28, 1, 1], 1)]:
            tests = [{"number": i, "seconds": s} for i, s in enumerate(durations)]
            workers = schedule(tests)
            self.assertEqual(len(workers), expected)
            self.assertEqual(sorted(t for worker in workers for t in worker), list(range(len(tests))))


class OperatorProofTests(unittest.TestCase):
    @staticmethod
    def facts(slot="object", *, none=False, identity=0, values=None, ne=None):
        return {"eq": slot, "ne": slot if ne is None else ne, "none": none,
                "type_identity": identity, "enum_values": values}

    def test_known_none_relations_match_python(self):
        values = [object(), None, "", "x", 0, 1, True, 2.5, float("nan"),
                  b"", [], [1], (), (1,), {}, {1: 2}, set(), {1}, frozenset({1})]
        for value in values:
            slot = "int" if isinstance(value, bool) else type(value).__name__
            if slot == "NoneType":
                slot = "object"
            a, b = self.facts(slot, none=value is None), self.facts(none=True)
            self.assertTrue(identity_equivalent_types(a, b, False, False, False))
            self.assertTrue(identity_equivalent_types(a, b, False, False, True))
            self.assertEqual(value == None, value is None)
            self.assertEqual(None == value, None is value)
            self.assertEqual(value != None, value is not None)

    def test_custom_comparisons_and_plain_strings_are_rejected(self):
        none = self.facts("custom", none=True)
        self.assertTrue(identity_equivalent_types(self.facts(), none, False, False, True))
        self.assertTrue(identity_equivalent_types(none, none, False, False, False))
        self.assertFalse(identity_equivalent_types(self.facts("custom"), none, False, False, False))
        self.assertFalse(identity_equivalent_types(self.facts(ne="custom"), none, False, False, True))
        self.assertFalse(identity_equivalent_types(self.facts("str"), self.facts("str"), False, False, False))

    def test_enum_requires_unique_values_and_registered_members(self):
        good = self.facts("str", identity=7, values=["A", "B"])
        self.assertTrue(identity_equivalent_types(good, good, True, True, False))
        self.assertFalse(identity_equivalent_types(good, good, True, False, False))
        duplicate = self.facts("str", identity=7, values=["same", "same"])
        self.assertFalse(identity_equivalent_types(duplicate, duplicate, True, True, False))
        other = self.facts("str", identity=8, values=["A", "B"])
        self.assertFalse(identity_equivalent_types(good, other, True, True, False))

    def test_ast_matcher_rejects_polarity_and_additional_changes(self):
        tree = ast.parse("def f(a):\n    return a is None\n")
        self.assertEqual(identity_edits(tree, ast.parse("def f(a):\n    return a == None\n")), [(2, 11, False)])
        self.assertEqual(identity_edits(tree, ast.parse("def f(a):\n    return a != None\n")), [])
        self.assertEqual(identity_edits(tree, ast.parse("def f(b):\n    return b == None\n")), [])
        self.assertEqual(identity_edits(ast.parse("x = (a is None, True)"),
                                       ast.parse("x = (a == None, 1)")), [])


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.text = "def f(a, b):\n    if (a\n        and b):\n        return 1\n    return 0\n"
        (self.root / "module.py").write_text(self.text)
        self.tests = [{"id": "tests/t.py::C::test_a", "number": 7, "seconds": .1},
                      {"id": "tests/t.py::C::test_b", "number": 9, "seconds": .1}]
        self.map = {"ids": [t["id"] for t in self.tests],
                    "lines": {"module.py": {"2": "3", "3": "2", "4": "2", "5": "1"}},
                    "modules": {"module.py": "3"}, "reads": {}, "symbols": {}, "source_hashes": {}}

    def selector(self):
        path = self.root / "map.json.gz"
        path.write_bytes(gzip.compress(json.dumps(self.map).encode()))
        return Selector(self.root, path, self.tests)

    def selected(self, new):
        return {t["number"] for t in self.selector().select(diff(self.text, new))}

    def test_boolean_operator_uses_entire_condition(self):
        self.assertEqual(self.selected(self.text.replace("and b", "or b")), {7, 9})

    def test_executed_body_line_is_precise(self):
        self.assertEqual(self.selected(self.text.replace("return 1", "return 2")), {9})

    def test_added_code_uses_enclosing_function(self):
        self.assertEqual(self.selected(self.text.replace("    return 0", "    print(a)\n    return 0")), {7, 9})

    def test_comments_keep_source_readers(self):
        self.map["reads"] = {"module.py": "1"}
        self.assertEqual(self.selected("# comment\n" + self.text), {7})

    def test_comments_do_not_hide_other_files(self):
        fixture = "tests/fixtures/example.sql"
        self.map["reads"][fixture] = "2"
        patch = diff(self.text, "# comment\n" + self.text) + diff("SELECT 1;\n", "SELECT 2;\n", fixture)
        self.assertEqual({t["number"] for t in self.selector().select(patch)}, {9})

    def test_syntax_errors_are_conservative(self):
        self.assertEqual(self.selected(self.text.replace("return 1", "return (")), {7, 9})

    def test_import_time_helper_retains_consumers(self):
        self.map["collection_lines"] = {"module.py": [4]}
        self.assertEqual(self.selected(self.text.replace("return 1", "return 2")), {7, 9})

    def test_unknown_test_identity_is_retained(self):
        self.tests.append({"id": "new_test", "number": 10, "seconds": 0})
        self.assertEqual(self.selected("# comment\n" + self.text), {10})

    def test_import_time_error_retains_every_test(self):
        self.text = "class Config:\n    value = 1\n"
        (self.root / "module.py").write_text(self.text)
        self.map["symbols"] = {"module.py:Config": "1"}
        self.map["lines"] = {}
        self.assertEqual(self.selected(self.text.replace("value = 1", "value = 1 / 0")), {7, 9})

    def configure_class_field(self):
        self.text = "class Config:\n    FLAG: int = 1\n"
        (self.root / "module.py").write_text(self.text)
        self.map["lines"] = {}
        self.map["symbols"] = {"module.py:Config": "3"}
        self.map["attributes"] = {"module.py:Config:FLAG": "2"}

    def test_literal_class_field_uses_observed_readers(self):
        self.configure_class_field()
        self.assertEqual(self.selected(self.text.replace("= 1", "= 2")), {9})

    def test_collection_read_class_field_retains_indirect_consumers(self):
        self.configure_class_field()
        self.map["collection_attributes"] = ["module.py:Config:FLAG"]
        self.assertEqual(self.selected(self.text.replace("= 1", "= 2")), {7, 9})

    def test_class_field_call_is_not_assumed_side_effect_free(self):
        self.configure_class_field()
        self.assertEqual(self.selected(self.text.replace("= 1", "= int('2')")), {7, 9})

    def test_changed_annotation_is_not_limited_to_field_readers(self):
        self.configure_class_field()
        self.assertEqual(self.selected(self.text.replace("FLAG: int", "FLAG: str")), {7, 9})

    def configure_profile(self):
        self.text = "def f(a):\n    return a is None\n"
        (self.root / "module.py").write_text(self.text)
        self.map["lines"] = {"module.py": {"2": "3"}}
        self.map["source_commit"] = "test-commit"
        facts = OperatorProofTests.facts
        self.profile = {"complete": True, "failures": [], "source_commit": "test-commit",
                        "ids": self.map["ids"], "types": [facts("int"), facts(none=True)],
                        "comparisons": {"module.py:2:11": [[0, 1, False, False]]}}

    def save_profile(self):
        (self.root / "type-profiles.json.gz").write_bytes(gzip.compress(json.dumps(self.profile).encode()))

    def test_profile_equivalence_retains_source_readers(self):
        self.configure_profile()
        self.map["reads"] = {"module.py": "1"}
        self.save_profile()
        self.assertEqual(self.selected(self.text.replace("is None", "== None")), {7})

    def test_profile_rejects_missing_sites_and_custom_comparisons(self):
        self.configure_profile()
        self.profile["types"][0]["eq"] = "custom"
        self.save_profile()
        self.assertEqual(self.selected(self.text.replace("is None", "== None")), {7, 9})
        self.profile["comparisons"] = {}
        self.save_profile()
        self.assertEqual(self.selected(self.text.replace("is None", "== None")), {7, 9})

    def test_incomplete_and_wrong_source_profiles_are_ignored(self):
        self.configure_profile()
        for key, value in [("complete", False), ("ids", self.map["ids"][:1]),
                           ("source_commit", "another-commit")]:
            original = self.profile[key]
            self.profile[key] = value
            self.save_profile()
            self.assertEqual(self.selected(self.text.replace("is None", "== None")), {7, 9})
            self.profile[key] = original

    def configure_reflection(self, field, collection=False):
        self.map["source_commit"] = "test-commit"
        payload = {"source_commit": "test-commit", "complete": True, "failures": [], "ids": self.map["ids"],
                   "functions": [{"path": "module.py", "line": 1, "name": "f",
                                  "fields": {field: "1"}, "collection": [field] if collection else []}]}
        (self.root / "reflection-fields.json.gz").write_bytes(gzip.compress(json.dumps(payload).encode()))

    def test_bytecode_reader_is_retained_without_function_execution(self):
        self.configure_reflection("co_consts")
        self.assertEqual(self.selected(self.text.replace("return 1", "return 2")), {7, 9})

    def test_signature_reader_does_not_depend_on_body_constants(self):
        self.configure_reflection("co_argcount")
        self.assertEqual(self.selected(self.text.replace("return 1", "return 2")), {9})

    def test_reflection_reader_survives_semantic_equivalence(self):
        self.configure_profile()
        self.save_profile()
        self.configure_reflection("co_code")
        self.assertEqual(self.selected(self.text.replace("is None", "== None")), {7})

    def test_line_number_reader_survives_comment_only_change(self):
        self.configure_reflection("co_firstlineno")
        self.assertEqual(self.selected("# comment\n" + self.text), {7})

    def test_collection_reflection_retains_indirect_consumers(self):
        self.configure_reflection("co_consts", collection=True)
        self.assertEqual(self.selected(self.text.replace("return 1", "return 2")), {7, 9})


if __name__ == "__main__":
    unittest.main()
