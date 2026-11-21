import ast
import copy
import gc
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
            scope.update(helpers(lambda obj, key, kind=None: recorded.append(
                (obj, key, kind) if kind else (obj, key))))
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

    def test_membership_observes_keys_without_reading_attribute_values(self):
        class Subject:
            FLAG = 1
        result, reads = self.run_source("""
present = 'FLAG' in Subject.__dict__
Subject.FLAG = object()
still_present = 'FLAG' in Subject.__dict__
absent = 'MISSING' not in Subject.__dict__
missing = 'MISSING' in Subject.__dict__
""", Subject=Subject)
        self.assertEqual([result[k] for k in ('present', 'still_present', 'absent', 'missing')],
                         [True, True, True, False])
        self.assertEqual(reads, [(Subject, None, "keys")] * 4)

    def test_membership_preserves_key_receiver_namespace_and_contains_order(self):
        script = """
events = []
class Mapping:
    def __contains__(self, key):
        events.append(('contains', key))
        return True
class Subject:
    @property
    def __dict__(self):
        events.append('namespace')
        return Mapping()
def key():
    events.append('key')
    return 'A'
def receiver():
    events.append('receiver')
    return Subject()
answer = key() not in receiver().__dict__
"""
        original, _ = self.run_source(script, observe=False)
        instrumented, reads = self.run_source(script)
        self.assertEqual(instrumented["events"], original["events"])
        self.assertEqual(instrumented["answer"], original["answer"])
        self.assertEqual(len(reads), 1)
        self.assertIsNone(reads[0][1])
        self.assertEqual(len(reads[0]), 2)

    def test_membership_custom_metaclass_and_custom_keys_stay_opaque(self):
        class Meta(type):
            @property
            def __dict__(cls):
                return types.MappingProxyType({"FLAG": 1} if cls.FLAG else {})
        class DerivedNamespace(metaclass=Meta):
            FLAG = True
        class Ordinary:
            FLAG = 1
        class Key(str):
            pass
        for subject, key in [(DerivedNamespace, "FLAG"), (Ordinary, Key("FLAG")), (Ordinary(), "FLAG")]:
            with self.subTest(subject=subject):
                result, reads = self.run_source("answer = key in Subject.__dict__", Subject=subject, key=key)
                self.assertEqual(result['answer'], key in subject.__dict__)
                self.assertEqual(reads, [(subject, None)])
        DerivedNamespace.FLAG = False
        result, reads = self.run_source("answer = 'FLAG' in Subject.__dict__", Subject=DerivedNamespace)
        self.assertFalse(result['answer'])
        self.assertEqual(reads, [(DerivedNamespace, None)])

    def test_membership_chains_preserve_short_circuit_and_remain_opaque(self):
        class Subject:
            FLAG = 1
        result, reads = self.run_source("answer = False == 'FLAG' in Subject.__dict__", Subject=Subject)
        self.assertFalse(result['answer'])
        self.assertEqual(reads, [])
        result, reads = self.run_source("answer = 'FLAG' in Subject.__dict__ == namespace",
                                       Subject=Subject, namespace=Subject.__dict__)
        self.assertTrue(result['answer'])
        self.assertEqual(reads, [(Subject, None)])

    def test_stored_custom_keys_can_depend_on_another_class_field(self):
        class DerivedKey(str):
            def __new__(cls, enabled):
                key = str.__new__(cls, "probe")
                key.enabled = enabled
                return key
            __hash__ = str.__hash__
            def __eq__(self, other):
                return self.enabled and str.__eq__(self, other)
        class Meta(type):
            def __new__(meta, name, bases, namespace):
                namespace[DerivedKey(namespace["FLAG"])] = 23
                return super().__new__(meta, name, bases, namespace)
        for enabled in (False, True):
            class Subject(metaclass=Meta):
                FLAG = enabled
            result, reads = self.run_source("""
present = 'probe' in Subject.__dict__
absent = 'probe' not in Subject.__dict__
value = Subject.__dict__.get('probe')
""", Subject=Subject)
            self.assertEqual(result["present"], enabled)
            self.assertEqual(result["absent"], not enabled)
            self.assertEqual(result["value"], 23 if enabled else None)
            self.assertEqual(reads, [(Subject, None)] * 3)

    def test_get_rechecks_stored_keys_after_default_argument_evaluation(self):
        for observe in (False, True):
            events = []
            class Key(str):
                __hash__ = str.__hash__
                def __eq__(self, other):
                    events.append("eq")
                    return str.__eq__(self, other)
            class Subject:
                FLAG = 1
            def default():
                events.append("default")
                # A real class mappingproxy remains live even after its get
                # method was fetched. No replacement namespace is involved.
                namespace = gc.get_referents(Subject.__dict__)[0]
                namespace[Key("probe")] = 23
                return 99
            result, reads = self.run_source("answer = Subject.__dict__.get('probe', default())",
                                           observe=observe, Subject=Subject, default=default)
            self.assertEqual(result["answer"], 23)
            self.assertEqual(events, ["default", "eq"])
            self.assertEqual(reads, [(Subject, None)] if observe else [])

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

    def test_v2_classifies_membership_and_preserves_overlapping_value_dependencies(self):
        profiles = self.inputs()
        for profile in profiles:
            profile['format_version'] = 2
            profile['keys'] = {'readers': {'m.py:C': '3'}, 'collection': ['m.py:C']}
            profile['all']['readers']['m.py:C'] = '3'
        profiles[1]['opaque'] = {'readers': {'m.py:C': '2'}, 'collection': ['m.py:C']}
        merged = merge_profiles(profiles)
        self.assertEqual(merged['format_version'], 2)
        self.assertEqual(merged['keys']['readers'], {'m.py:C': '3'})
        self.assertEqual(merged['fields']['readers'], {'m.py:C:FLAG': '1'})
        self.assertEqual(merged['opaque']['readers'], {'m.py:C': '2'})
        self.assertEqual(merged['keys']['collection'], ['m.py:C'])

    def test_legacy_profiles_remain_compatible_but_versions_cannot_mix(self):
        legacy = merge_profiles(self.inputs())
        self.assertEqual(legacy['format_version'], 1)
        self.assertNotIn('keys', legacy)
        profiles = self.inputs()
        profiles[0]['format_version'] = 2
        profiles[0]['keys'] = {'readers': {}, 'collection': []}
        with self.assertRaises(ValueError):
            merge_profiles(profiles)

    def test_v2_requires_complete_membership_catalog_and_known_version(self):
        for value in (0, 3, True, '2'):
            profiles = self.inputs()
            profiles[0]['format_version'] = value
            with self.subTest(version=value), self.assertRaises(ValueError):
                merge_profiles(profiles)
        for category in (None, {}, {'readers': {}, 'collection': []},
                         {'readers': {'m.py:C': '4'}, 'collection': []},
                         {'readers': {'m.py:C': '2'}, 'collection': ['m.py:Missing']}):
            profiles = self.inputs()
            for profile in profiles:
                profile['format_version'] = 2
                profile['all']['readers']['m.py:C'] = '3'
                if category is not None:
                    profile['keys'] = copy.deepcopy(category)
            with self.subTest(keys=category), self.assertRaises(ValueError):
                merge_profiles(profiles)
        profiles = self.inputs()
        profiles[0]['keys'] = {'readers': {}, 'collection': []}
        with self.assertRaises(ValueError):
            merge_profiles(profiles)


if __name__ == "__main__":
    unittest.main()
