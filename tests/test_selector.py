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

    def test_replacement_can_move_execution_out_of_a_rare_branch(self):
        self.assertEqual(self.selected(self.text.replace("        return 1", "    return 1")), {7, 9})

    def test_replacement_can_move_execution_to_module_scope(self):
        self.text = "def f(a):\n    if a:\n        raise RuntimeError()\n"
        (self.root / "module.py").write_text(self.text)
        self.map["lines"] = {"module.py": {"2": "1", "3": "1"}}
        self.map["modules"] = {"module.py": "1"}
        self.assertEqual(self.selected(self.text.replace("        raise RuntimeError()", "        pass\nraise RuntimeError()")), {7, 9})

    def test_binding_in_rare_branch_affects_whole_function(self):
        self.text = "def f(a):\n    if isinstance(a, str):\n        return 1\n    return 0\n"
        (self.root / "module.py").write_text(self.text)
        self.map["lines"] = {"module.py": {"2": "3", "3": "2", "4": "1"}}
        self.assertEqual(self.selected(self.text.replace("return 1", "return (isinstance := None)")), {7, 9})

    def test_lexical_change_retains_collection_consumers(self):
        self.map["collection_lines"] = {"module.py": [5]}
        self.assertEqual(self.selected(self.text.replace("return 1", "return (hidden := 1)")), {7, 9})

    def configure_lambdas(self):
        self.text = "class Dispatch:\n    TABLE = {\n        'a': lambda: 1,\n        'b': lambda: 2,\n    }\n"
        (self.root / "module.py").write_text(self.text)
        self.map["lines"] = {"module.py": {"3": "1", "4": "2"}}
        self.map["symbols"] = {"module.py:Dispatch": "3"}

    def test_lambda_body_change_keeps_precise_selection(self):
        self.configure_lambdas()
        # Definition-time coverage does not prove that the body was called.
        self.map["collection_lines"] = {"module.py": [3, 4]}
        self.assertEqual(self.selected(self.text.replace("lambda: 2", "lambda: 3")), {9})

    def save_lambda_call_profile(self, readers=None, **overrides):
        self.map["source_commit"] = "test-commit"
        payload = {"source_commit": "test-commit", "ids": self.map["ids"], "complete": True,
                   "source_hashes": {path: value for path, value in self.map["source_hashes"].items()
                                     if path.startswith("sqlglot/")},
                   "modes": ["warm", "cold", "eager-import-collection"],
                   "failures": [], "readers": readers if readers is not None else {"module.py:4:13": "2"},
                   "collection": []}
        payload.update(overrides)
        (self.root / "lambda-call-readers.json.gz").write_bytes(gzip.compress(json.dumps(payload).encode()))

    def test_lambda_call_profile_excludes_definition_only_importer(self):
        self.configure_lambdas()
        self.map["lines"]["module.py"]["4"] = "3"
        self.save_lambda_call_profile()
        self.assertEqual(self.selected(self.text.replace("lambda: 2", "lambda: 3")), {9})
        self.map["reads"]["module.py"] = "1"
        self.assertEqual(self.selected(self.text.replace("lambda: 2", "lambda: 3")), {7, 9})

    def test_lambda_call_profile_missing_site_keeps_line_coverage(self):
        self.configure_lambdas()
        self.map["lines"]["module.py"]["4"] = "3"
        self.save_lambda_call_profile(readers={})
        self.assertEqual(self.selected(self.text.replace("lambda: 2", "lambda: 3")), {7, 9})

    def test_lambda_call_manifest_covers_library_not_test_files(self):
        self.configure_lambdas()
        self.map["lines"]["module.py"]["4"] = "3"
        self.map["source_hashes"]["tests/test_other.py"] = "test-file-hash"
        self.save_lambda_call_profile()
        self.assertEqual(self.selected(self.text.replace("lambda: 2", "lambda: 3")), {9})

    def test_untrustworthy_lambda_call_profile_keeps_line_coverage(self):
        self.configure_lambdas()
        self.map["lines"]["module.py"]["4"] = "3"
        for key, value in [("complete", False), ("source_commit", "wrong"), ("ids", []),
                           ("source_hashes", {"module.py": "wrong"}), ("modes", ["warm"]),
                           ("failures", ["failed-test"])]:
            self.save_lambda_call_profile(**{key: value})
            self.assertEqual(self.selected(self.text.replace("lambda: 2", "lambda: 3")), {7, 9})

    def test_lambda_profile_collection_call_retains_indirect_consumers(self):
        self.configure_lambdas()
        self.save_lambda_call_profile(collection=["module.py:4:13"])
        self.assertEqual(self.selected(self.text.replace("lambda: 2", "lambda: 3")), {7, 9})

    def test_lambda_profile_zero_call_record_is_distinct_from_missing_site(self):
        self.configure_lambdas()
        self.save_lambda_call_profile(readers={"module.py:4:13": "0"})
        self.assertEqual(self.selected(self.text.replace("lambda: 2", "lambda: 3")), set())

    def test_lambda_profile_unions_multiple_bodies_on_one_line(self):
        self.text = "table = (lambda: 1, lambda: 2)\n"
        (self.root / "module.py").write_text(self.text)
        self.map["lines"] = {"module.py": {"1": "3"}}
        self.save_lambda_call_profile(readers={"module.py:1:9": "1", "module.py:1:20": "2"})
        self.assertEqual(self.selected(self.text.replace("lambda: 2", "lambda: 3")), {7, 9})

    def test_multiline_factory_lambda_keeps_later_inner_callers(self):
        self.text = "factory = lambda: (\n    lambda: 1,\n    lambda: 2,\n)[0]\n"
        (self.root / "module.py").write_text(self.text)
        # Test 7 calls the factory; test 9 later executes its saved result.
        self.map["lines"] = {"module.py": {"1": "1", "2": "2", "3": "0", "4": "1"}}
        self.save_lambda_call_profile(readers={"module.py:1:10": "1", "module.py:2:4": "2",
                                               "module.py:3:4": "0"})
        self.assertEqual(self.selected(self.text.replace(")[0]", ")[1]")), {7, 9})

    def test_factory_lambda_keeps_later_generator_consumers(self):
        self.text = "factory = lambda: (\n    value + 1\n    for value in range(3)\n)\n"
        (self.root / "module.py").write_text(self.text)
        self.map["lines"] = {"module.py": {"1": "1", "2": "2", "3": "3", "4": "1"}}
        self.save_lambda_call_profile(readers={"module.py:1:10": "1"})
        self.assertEqual(self.selected(self.text.replace("value + 1", "value + 2")), {7, 9})

    def test_collection_lambda_call_retains_indirect_consumers(self):
        self.configure_lambdas()
        self.map["collection_lambda_bodies"] = {"module.py": [4]}
        self.assertEqual(self.selected(self.text.replace("lambda: 2", "lambda: 3")), {7, 9})

    def test_collection_lambda_profile_requires_matching_complete_source(self):
        self.configure_lambdas()
        self.map["source_commit"] = "test-commit"
        profile = {"source_commit": "test-commit", "complete": True, "ids": self.map["ids"],
                   "lambda_bodies": {"module.py": [4]}}
        path = self.root / "collection-profile.json.gz"
        path.write_bytes(gzip.compress(json.dumps(profile).encode()))
        self.assertEqual(self.selected(self.text.replace("lambda: 2", "lambda: 3")), {7, 9})
        for key, value in [("complete", False), ("source_commit", "wrong"), ("ids", [])]:
            invalid = dict(profile, **{key: value})
            path.write_bytes(gzip.compress(json.dumps(invalid).encode()))
            self.assertEqual(self.selected(self.text.replace("lambda: 2", "lambda: 3")), {9})

    def test_dispatch_key_on_lambda_line_retains_other_dispatch_users(self):
        self.configure_lambdas()
        self.assertEqual(self.selected(self.text.replace("'b':", "'a':")), {7, 9})

    def test_lambda_default_on_body_line_is_import_checked(self):
        self.configure_lambdas()
        self.assertEqual(self.selected(self.text.replace("lambda: 2", "lambda a=1/0: 2")), {7, 9})

    def test_comments_keep_source_readers(self):
        self.map["reads"] = {"module.py": "1"}
        self.assertEqual(self.selected("# comment\n" + self.text), {7})

    def test_collection_source_readers_survive_ast_equivalent_edits(self):
        self.map["collection_reads"] = ["module.py"]
        self.assertEqual(self.selected("# comment\n" + self.text), {7, 9})

    def test_collection_fixture_read_retains_indirect_consumers(self):
        fixture = "tests/fixtures/example.sql"
        self.map["reads"][fixture] = "1"
        self.map["collection_reads"] = [fixture]
        patch = diff("SELECT 1;\n", "SELECT 2;\n", fixture)
        self.assertEqual({t["number"] for t in self.selector().select(patch)}, {7, 9})

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

    def test_module_value_can_escape_through_an_imported_alias(self):
        self.text = "FIXTURE_DIR = 'fixtures'\n\ndef unused():\n    return FIXTURE_DIR\n"
        (self.root / "module.py").write_text(self.text)
        self.map["lines"] = {}
        self.map["modules"] = {"module.py": "1"}
        self.assertEqual(self.selected(self.text.replace("'fixtures'", "'missing'")), {7, 9})

    def test_new_module_assignment_at_eof_or_in_a_gap_retains_alias_consumers(self):
        self.text = "FIXTURE_DIR = 'fixtures'\n\n\ndef unused():\n    return FIXTURE_DIR\n"
        (self.root / "module.py").write_text(self.text)
        self.map["lines"] = {}
        self.map["modules"] = {"module.py": "1"}
        for changed in [self.text + "\nFIXTURE_DIR = 'missing'\n",
                        self.text.replace("\n\n\n", "\n\nFIXTURE_DIR = 'missing'\n\n")]:
            self.assertEqual(self.selected(changed), {7, 9})

    def test_module_import_alias_change_retains_indirect_consumers(self):
        self.text = "from os.path import join as build_path\n"
        (self.root / "module.py").write_text(self.text)
        self.map["lines"] = {}
        self.map["modules"] = {"module.py": "1"}
        self.assertEqual(self.selected(self.text.replace("join", "dirname")), {7, 9})

    def configure_class_field(self):
        self.text = "class Config:\n    FLAG: int = 1\n"
        (self.root / "module.py").write_text(self.text)
        self.map["lines"] = {}
        self.map["symbols"] = {"module.py:Config": "3"}
        self.map["attributes"] = {"module.py:Config:FLAG": "2"}

    def test_literal_class_field_uses_observed_readers(self):
        self.configure_class_field()
        self.assertEqual(self.selected(self.text.replace("= 1", "= 2")), {9})

    def test_annotation_only_declaration_does_not_interrupt_literal_selection(self):
        self.configure_class_field()
        self.text = "class Config:\n    OTHER: int\n    FLAG = 1\n"
        (self.root / "module.py").write_text(self.text)
        self.assertEqual(self.selected(self.text.replace("= 1", "= 2")), {9})

    def test_multiple_literal_fields_on_one_line_union_their_consumers(self):
        self.configure_class_field()
        self.text = "class Config:\n    FLAG = 1; OTHER = 1\n"
        (self.root / "module.py").write_text(self.text)
        self.map["attributes"] = {"module.py:Config:FLAG": "1", "module.py:Config:OTHER": "2"}
        self.assertEqual(self.selected(self.text.replace("= 1", "= 2")), {7, 9})

    def test_literal_precision_rejects_other_changes_on_the_same_line(self):
        self.configure_class_field()
        self.text = "def produce():\n    return 1\n\nclass Config:\n    FLAG = 1; OTHER = produce()\n"
        (self.root / "module.py").write_text(self.text)
        self.assertEqual(self.selected(self.text.replace("FLAG = 1; OTHER = produce()",
                                                        "FLAG = 2; OTHER = produce() + 1")), {7, 9})

    def test_literal_eval_set_call_can_resolve_to_a_shadowed_function(self):
        self.configure_class_field()
        self.text = "def set():\n    return 'effect'\n\nclass Config:\n    FLAG = 1\n"
        (self.root / "module.py").write_text(self.text)
        self.assertEqual(self.selected(self.text.replace("FLAG = 1", "FLAG = set()")), {7, 9})

    def test_bare_class_field_reads_retain_derived_consumers(self):
        for consumer in ["    DERIVED = str(FLAG)\n",
                         "    DERIVED = {str(value): value for value in (FLAG,)}\n",
                         "    def method(self, value=FLAG):\n        return value\n",
                         "    @staticmethod if FLAG else classmethod\n    def method():\n        pass\n",
                         "    class Nested(object if FLAG else int):\n        pass\n",
                         "    class Nested(metaclass=type if FLAG else type):\n        pass\n",
                         "    def method(self):\n        return FLAG\n"]:
            with self.subTest(consumer=consumer):
                self.configure_class_field()
                self.text += consumer
                (self.root / "module.py").write_text(self.text)
                # Namespace precision cannot observe Python's class-local loads.
                self.save_class_dict_profile(collection=["module.py:Config"])
                self.save_namespace_profile()
                selector = self.selector()
                selected = selector.select(diff(self.text, self.text.replace("= 1", "= 2")))
                self.assertEqual({test["number"] for test in selected}, {7, 9})
                self.assertEqual(selector.literal_readers["module.py"], {})

    def test_attribute_access_does_not_disable_field_precision(self):
        self.configure_class_field()
        self.text += "    def method(self):\n        return self.FLAG\n"
        (self.root / "module.py").write_text(self.text)
        self.assertEqual(self.selected(self.text.replace("= 1", "= 2")), {9})

    def test_new_bare_class_field_dependency_disables_field_precision(self):
        self.configure_class_field()
        self.text += "    DERIVED = 0\n"
        (self.root / "module.py").write_text(self.text)
        selector = self.selector()
        modified = self.text.replace("= 1", "= 2").replace("DERIVED = 0", "DERIVED = FLAG")
        self.assertEqual({test["number"] for test in selector.select(diff(self.text, modified))}, {7, 9})
        self.assertEqual(selector.literal_readers["module.py"], {})

    def save_class_dict_profile(self, **overrides):
        self.map["source_commit"] = "test-commit"
        payload = {"source_commit": "test-commit", "ids": self.map["ids"], "complete": True,
                   "failures": [], "readers": {"module.py:Config": "1"}, "collection": []}
        payload.update(overrides)
        (self.root / "class-dict-readers.json.gz").write_bytes(gzip.compress(json.dumps(payload).encode()))

    def test_literal_class_field_retains_direct_dictionary_readers(self):
        self.configure_class_field()
        self.save_class_dict_profile()
        self.assertEqual(self.selected(self.text.replace("= 1", "= 2")), {7, 9})

    def test_collection_class_dict_blocks_field_narrowing(self):
        self.configure_class_field()
        self.save_class_dict_profile(readers={}, collection=["module.py:Config"])
        self.assertEqual(self.selected(self.text.replace("= 1", "= 2")), {7, 9})

    def save_namespace_profile(self, **overrides):
        payload = {"source_commit": self.map["source_commit"], "ids": self.map["ids"],
                   "source_hashes": self.map["source_hashes"], "complete": True, "failures": [],
                   "modes": ["warm", "cold", "eager-import-collection"],
                   "all": {"readers": {"module.py:Config": "1"}, "collection": ["module.py:Config"]},
                   "opaque": {"readers": {}, "collection": []},
                   "fields": {"readers": {"module.py:Config:OTHER": "1"}, "collection": ["module.py:Config:OTHER"]}}
        payload.update(overrides)
        (self.root / "namespace-readers.json.gz").write_bytes(gzip.compress(json.dumps(payload).encode()))

    def test_namespace_key_reads_do_not_depend_on_other_literal_fields(self):
        self.configure_class_field()
        self.save_class_dict_profile(collection=["module.py:Config"])
        self.save_namespace_profile()
        self.assertEqual(self.selected(self.text.replace("= 1", "= 2")), {9})

    def test_namespace_value_key_and_opaque_consumers_are_retained(self):
        self.configure_class_field()
        self.save_class_dict_profile(collection=["module.py:Config"])
        for category, key in [("fields", "module.py:Config:FLAG"), ("opaque", "module.py:Config")]:
            self.save_namespace_profile(**{category: {"readers": {key: "1"}, "collection": []}})
            self.assertEqual(self.selected(self.text.replace("= 1", "= 2")), {7, 9})
            self.save_namespace_profile(**{category: {"readers": {}, "collection": [key]}})
            self.assertEqual(self.selected(self.text.replace("= 1", "= 2")), {7, 9})

    def test_namespace_profile_cannot_remove_unobserved_historical_contexts(self):
        self.configure_class_field()
        self.save_class_dict_profile(collection=["module.py:Config"])
        for all_reads in [{"readers": {}, "collection": ["module.py:Config"]},
                          {"readers": {"module.py:Config": "1"}, "collection": []}]:
            self.save_namespace_profile(all=all_reads)
            self.assertEqual(self.selected(self.text.replace("= 1", "= 2")), {7, 9})

    def test_namespace_profile_keeps_direct_import_time_attribute_dependency(self):
        self.configure_class_field()
        self.save_class_dict_profile(collection=["module.py:Config"])
        self.save_namespace_profile()
        self.map["collection_attributes"] = ["module.py:Config:FLAG"]
        self.assertEqual(self.selected(self.text.replace("= 1", "= 2")), {7, 9})

    def test_namespace_profile_requires_all_modes_and_matching_complete_baseline(self):
        self.configure_class_field()
        self.save_class_dict_profile(collection=["module.py:Config"])
        for key, value in [("complete", False), ("failures", ["failed"]), ("ids", []),
                           ("source_commit", "wrong"), ("source_hashes", {"other.py": "wrong"}),
                           ("modes", ["warm", "cold"])]:
            self.save_namespace_profile(**{key: value})
            self.assertEqual(self.selected(self.text.replace("= 1", "= 2")), {7, 9})

    def test_class_dict_readers_survive_missing_class_symbol_map(self):
        self.configure_class_field()
        self.map["symbols"] = {}
        self.map["modules"] = {"module.py": "2"}
        self.save_class_dict_profile(collection=["module.py:Config"])
        self.assertEqual(self.selected(self.text.replace("= 1", "= 2")), {7, 9})

    def test_incomplete_or_mismatched_class_dict_profile_is_ignored(self):
        self.configure_class_field()
        for key, value in [("complete", False), ("source_commit", "wrong"), ("ids", []),
                           ("failures", ["test_failure"])]:
            self.save_class_dict_profile(**{key: value})
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

    def test_multifile_patch_does_not_reuse_baseline_operand_proof(self):
        self.configure_profile()
        self.save_profile()
        other = "def g():\n    return 1\n"
        (self.root / "other.py").write_text(other)
        self.map["lines"]["other.py"] = {"2": "1"}
        patch = diff(self.text, self.text.replace("is None", "== None")) + diff(
            other, other.replace("return 1", "return 2"), "other.py")
        self.assertEqual({t["number"] for t in self.selector().select(patch)}, {7, 9})

    def test_repeated_file_sections_fall_back_conservatively(self):
        patch = diff(self.text, self.text.replace("return 1", "return 2"))
        self.assertEqual({t["number"] for t in self.selector().select(patch + patch)}, {7, 9})

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
