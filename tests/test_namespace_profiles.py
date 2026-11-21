import ast
import copy
import types
import unittest

from tools.merge_namespace_profiles import merge_profiles
from tools.namespace_reads import NamespaceObserver, helpers


class NamespaceObserverTests(unittest.TestCase):
    @staticmethod
    def run_source(source, observe=True, **values):
        recorded = []
        scope = dict(values)
        if observe:
            scope.update(helpers(lambda obj, key: recorded.append((obj, key))))
            code = compile(ast.fix_missing_locations(NamespaceObserver().visit(ast.parse(source))), "test.py", "exec")
        else:
            code = compile(source, "test.py", "exec")
        exec(code, scope)
        return scope, recorded

    def test_specific_get_keeps_live_namespace_and_eager_default(self):
        class Subject:
            FLAG = 1
        effects = []

        def default():
            effects.append("default")
            Subject.FLAG = 2
            return 99

        result, reads = self.run_source("answer = Subject.__dict__.get('FLAG', default())", Subject=Subject, default=default)
        self.assertEqual(result["answer"], 2)
        self.assertEqual(effects, ["default"])
        self.assertEqual(reads, [(Subject, "FLAG")])

    def test_receiver_and_method_lookup_precede_argument_side_effects(self):
        script = """
events = []
class Mapping:
    def __getattribute__(self, name):
        if name == 'get':
            events.append('method')
        return object.__getattribute__(self, name)
    def get(self, key, default):
        events.append(('get', key, default))
        return 12
class Subject:
    @property
    def __dict__(self):
        events.append('namespace')
        return Mapping()
def receiver():
    events.append('receiver')
    return Subject()
def key():
    events.append('key')
    return 'A'
def default():
    events.append('default')
    return 7
answer = receiver().__dict__.get(key(), default())
"""
        original, _ = self.run_source(script, observe=False)
        instrumented, _ = self.run_source(script)
        self.assertEqual(instrumented["events"], original["events"])
        self.assertEqual(instrumented["answer"], original["answer"])

    def test_aliases_iteration_and_dynamic_namespace_access_stay_opaque(self):
        class Subject:
            FLAG = 1
        script = """
namespace = Subject.__dict__
alias_value = namespace.get('FLAG')
copied = dict(Subject.__dict__.items())
via_vars = vars(Subject)
via_getattr = getattr(Subject, '__dict__')
exists = hasattr(Subject, '__dict__')
"""
        result, reads = self.run_source(script, Subject=Subject)
        self.assertIs(type(result["namespace"]), types.MappingProxyType)
        self.assertEqual(result["alias_value"], 1)
        self.assertTrue(result["exists"])
        self.assertEqual(reads, [(Subject, None)] * 5)
        Subject.FLAG = 3
        self.assertEqual(result["namespace"]["FLAG"], 3)

    def test_keyword_errors_and_custom_string_keys_are_not_precise(self):
        class Subject:
            FLAG = 1
        class Key(str):
            pass
        result, reads = self.run_source("answer = Subject.__dict__.get(key)", Subject=Subject, key=Key("FLAG"))
        self.assertEqual(result["answer"], 1)
        self.assertEqual(reads, [(Subject, None)])
        for observe in (False, True):
            with self.assertRaises(TypeError):
                self.run_source("Subject.__dict__.get(key='FLAG')", Subject=Subject, observe=observe)

    def test_custom_metaclass_namespace_cannot_claim_specific_key(self):
        class Meta(type):
            @property
            def __dict__(cls):
                return types.MappingProxyType({"derived": cls.FLAG})
        class Subject(metaclass=Meta):
            FLAG = 1
        result, reads = self.run_source("answer = Subject.__dict__.get('derived')", Subject=Subject)
        self.assertEqual(result["answer"], 1)
        self.assertEqual(reads, [(Subject, None)])

    def test_custom_metaclass_attribute_lookup_stays_opaque(self):
        class Meta(type):
            def __getattribute__(cls, name):
                if name == "__dict__":
                    return types.MappingProxyType({"derived": type.__getattribute__(cls, "FLAG")})
                return type.__getattribute__(cls, name)
        class Subject(metaclass=Meta):
            FLAG = 1
        result, reads = self.run_source("answer = Subject.__dict__.get('derived')", Subject=Subject)
        self.assertEqual(result["answer"], 1)
        self.assertEqual(reads, [(Subject, None)])

    def test_shadowed_builtin_callable_is_not_compared_or_observed(self):
        class Replacement:
            def __call__(self, *args):
                return 7
            def __eq__(self, other):
                raise AssertionError("observer must not compare user callables")
        result, reads = self.run_source("answer = getattr(object(), '__dict__')", getattr=Replacement())
        self.assertEqual(result["answer"], 7)
        self.assertEqual(reads, [])


class NamespaceMergeTests(unittest.TestCase):
    @staticmethod
    def profile(mode="warm"):
        return {"source_commit": "baseline", "ids": ["a", "b"], "source_hashes": {"m.py": "sha"},
                "generator_hashes": {"namespace_reads.py": "observer-sha"},
                "mode": mode, "complete": True, "failures": [],
                "all": {"readers": {"m.py:C": "1"}, "collection": ["m.py:C"]},
                "opaque": {"readers": {}, "collection": []},
                "fields": {"readers": {"m.py:C:FLAG": "1"}, "collection": ["m.py:C:FLAG"]}}

    def inputs(self):
        return [self.profile(mode) for mode in ("warm", "cold", "eager-import-collection")]

    def test_union_preserves_every_specific_and_opaque_dependency(self):
        profiles = self.inputs()
        profiles[1]["all"]["readers"]["m.py:C"] = "2"
        profiles[1]["fields"]["readers"]["m.py:C:FLAG"] = "2"
        profiles[2]["opaque"] = {"readers": {"m.py:C": "1"}, "collection": ["m.py:C"]}
        original = copy.deepcopy(profiles)
        merged = merge_profiles(profiles)
        self.assertEqual(merged["all"]["readers"]["m.py:C"], "3")
        self.assertEqual(merged["fields"]["readers"]["m.py:C:FLAG"], "3")
        self.assertEqual(merged["opaque"]["readers"]["m.py:C"], "1")
        self.assertEqual(merged["opaque"]["collection"], ["m.py:C"])
        self.assertEqual(len(merged["runs"]), 3)
        self.assertEqual(profiles, original)

    def test_requires_all_modes_and_matching_complete_provenance(self):
        for omitted in range(3):
            with self.assertRaises(ValueError):
                merge_profiles([p for i, p in enumerate(self.inputs()) if i != omitted])
        for key, value in [("complete", False), ("complete", "true"), ("failures", ["failure"]), ("mode", "partial"),
                           ("source_commit", "other"), ("ids", ["b", "a"]),
                           ("source_hashes", {"m.py": "changed"}),
                           ("generator_hashes", {"namespace_reads.py": "changed"})]:
            profiles = self.inputs()
            profiles[1][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                merge_profiles(profiles)

    def test_rejects_unclassified_reads_missing_collection_and_invalid_bits(self):
        for category, bits in [("all", "3"), ("fields", "2"), ("fields", "4"), ("fields", "-1")]:
            profiles = self.inputs()
            key = "m.py:C:FLAG" if category == "fields" else "m.py:C"
            profiles[0][category]["readers"][key] = bits
            with self.subTest(category=category, bits=bits), self.assertRaises(ValueError):
                merge_profiles(profiles)
        profiles = self.inputs()
        profiles[0]["fields"]["collection"].append("m.py:C:UNKNOWN")
        with self.assertRaises(ValueError):
            merge_profiles(profiles)


if __name__ == "__main__":
    unittest.main()
