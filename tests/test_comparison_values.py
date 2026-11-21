import ast
import math
import unittest

from tools.comparison_values import ComparisonObserver, HELPER, ValueObserver, decode, encode
from tools.comparison_proof import comparison_edits, equivalent_comparisons
from tools.merge_comparison_profiles import MODES, merge_profiles


class ComparisonValueTests(unittest.TestCase):
    def test_supported_values_roundtrip_exactly(self):
        for value in (None, True, False, 0, -123, 2 ** 200, -0.0, 1.5, "abc", b"bytes",
                      (None, True, 2, ("nested", 0.5))):
            actual = decode(encode(value))
            self.assertIs(type(actual), type(value))
            self.assertEqual(actual, value)
        self.assertEqual(math.copysign(1, decode(encode(-0.0))), -1)

    def test_unknown_mutable_and_large_values_are_rejected(self):
        class IntSubclass(int):
            pass
        for value in ([], {}, set(), IntSubclass(1), float("nan"), float("inf"),
                      (float("nan"),), "a" * 257, 2 ** 257, tuple(range(13))):
            with self.assertRaises(ValueError):
                encode(value)

    def test_original_result_is_not_coerced_or_repeated(self):
        events = []
        result = object()
        class Custom:
            def __lt__(self, other):
                events.append("lt")
                return result
        observer = ValueObserver()
        self.assertIs(observer.compare("site", "Lt", Custom(), 1), result)
        self.assertEqual(events, ["lt"])
        self.assertTrue(observer.sites["site"]["opaque"])

    def test_exceptions_and_overflow_remain_opaque(self):
        observer = ValueObserver(limit=1)
        self.assertFalse(observer.compare("limit", "Lt", 1, 0))
        self.assertFalse(observer.compare("limit", "Lt", 2, 0))
        self.assertTrue(observer.sites["limit"]["opaque"])
        self.assertEqual(observer.sites["limit"]["observations"], set())
        with self.assertRaises(TypeError):
            observer.compare("error", "Lt", 1, None)
        self.assertTrue(observer.sites["error"]["opaque"])

    def test_preserves_operand_order_and_chained_short_circuit(self):
        source = "result = left() < right()\nchain = 3 < 2 < never()\n"
        tree = ast.fix_missing_locations(ComparisonObserver("module.py").visit(ast.parse(source)))
        events = []
        observer = ValueObserver()
        def left():
            events.append("left")
            return 1
        def right():
            events.append("right")
            return 2
        def never():
            self.fail("chain lost short circuit")
        scope = {HELPER: observer.compare, "left": left, "right": right, "never": never}
        exec(compile(tree, "module.py", "exec"), scope)
        self.assertEqual(events, ["left", "right"])
        self.assertIs(scope["result"], True)
        self.assertIs(scope["chain"], False)
        self.assertEqual(len(observer.sites), 1)


class ComparisonProofTests(unittest.TestCase):
    @staticmethod
    def proof(before, after, values, opaque=False):
        left, right = ast.parse(before), ast.parse(after)
        node = next(n for n in ast.walk(left) if isinstance(n, ast.Compare))
        observer = ValueObserver()
        site = f"module.py:{node.lineno}:{node.col_offset}"
        for a, b in values:
            observer.compare(site, type(node.ops[0]).__name__, a, b)
        if opaque:
            observer.sites[site]["opaque"] = True
        return equivalent_comparisons("module.py", left, right, {"sites": observer.sites})

    def test_operator_requires_all_baseline_results_unchanged(self):
        old, new = "result = value < 10", "result = value <= 10"
        self.assertTrue(self.proof(old, new, [(5, 10), (11, 10)]))
        self.assertFalse(self.proof(old, new, [(5, 10), (10, 10)]))
        self.assertFalse(self.proof(old, new, [(5, 10)], opaque=True))
        self.assertFalse(self.proof(old, new, []))

    def test_direct_literals_may_change_but_calls_must_remain(self):
        old = "result = value() < (2, 4)"
        self.assertTrue(self.proof(old, "result = value() < (2, 5)", [((1, 0), (2, 4))]))
        self.assertFalse(self.proof(old, "result = other() < (2, 4)", [((1, 0), (2, 4))]))
        self.assertFalse(self.proof(old, "result = value() < set()", [((1, 0), (2, 4))]))

    def test_other_edits_and_identity_are_rejected(self):
        old = "def f(value):\n    return value < 10\n"
        for new in ("def f(value):\n    return value <= 10\nside_effect()\n",
                    "def f(value):\n    return value is 10\n",
                    "def f(other):\n    return other < 10\n"):
            self.assertFalse(comparison_edits(ast.parse(old), ast.parse(new)))

    def test_constant_observation_must_match_old_operand(self):
        self.assertFalse(self.proof("result = value < 10", "result = value < 11", [(5, 9)]))

    def test_nested_mutations_and_chains_are_not_proved(self):
        self.assertFalse(comparison_edits(ast.parse("x = 1 < a < 3"), ast.parse("x = 1 <= a < 3")))
        self.assertFalse(comparison_edits(ast.parse("x = (a < 1) == b"), ast.parse("x = (a <= 1) == b")))

    def test_merge_rejects_missing_failed_or_mismatching_run(self):
        import copy
        header = {"source_commit": "pinned", "source_hashes": {"source": "hash"},
                  "generator_hashes": {"observer": "hash"}, "ids": ["test"],
                  "complete": True, "failures": [], "sites": {"site": {
                      "operation": "Lt", "opaque": False,
                      "observations": [(encode(1), encode(2), True)]}}}
        profiles = [{**copy.deepcopy(header), "mode": mode} for mode in sorted(MODES)]
        merged = merge_profiles(profiles)
        self.assertEqual(len(merged["sites"]["site"]["observations"]), 1)
        with self.assertRaises(ValueError):
            merge_profiles(profiles[:-1])
        for field, value in (("complete", False), ("ids", ["other"]), ("source_hashes", {})):
            changed = copy.deepcopy(profiles)
            changed[0][field] = value
            with self.assertRaises(ValueError):
                merge_profiles(changed)
        profiles[0]["sites"]["site"]["opaque"] = True
        self.assertTrue(merge_profiles(profiles)["sites"]["site"]["opaque"])


if __name__ == "__main__":
    unittest.main()
