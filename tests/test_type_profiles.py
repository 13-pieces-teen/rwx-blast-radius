import unittest

from tools.merge_type_profiles import merge_profiles


class TypeProfileMergeTests(unittest.TestCase):
    @staticmethod
    def profile(cold=False):
        return {"source_commit": "baseline", "ids": ["test"], "complete": True, "failures": [],
                "cold_caches": cold,
                "types": [{"type_identity": 0}, {"type_identity": 0}, {"type_identity": 1}],
                "comparisons": {"module.py:2:4": [[0, 1, True, True], [1, 2, False, False]]}}

    def test_preserves_pairs_and_isolates_process_type_identities(self):
        old, cold = self.profile(), self.profile(True)
        merged = merge_profiles([old, cold])
        self.assertEqual([info["type_identity"] for info in merged["types"]], [0, 0, 1, 2, 2, 3])
        self.assertEqual(merged["comparisons"]["module.py:2:4"],
                         [[0, 1, True, True], [1, 2, False, False],
                          [3, 4, True, True], [4, 5, False, False]])
        self.assertEqual(merged["cache_modes"], ["warm", "cold"])
        self.assertEqual(cold, self.profile(True))

    def test_rejects_untrustworthy_inputs(self):
        for field, value in [("complete", False), ("failures", ["failure"]),
                             ("source_commit", "other"), ("ids", ["other"])]:
            changed = self.profile()
            changed[field] = value
            with self.assertRaises(ValueError):
                merge_profiles([self.profile(), changed])
        changed = self.profile()
        changed["comparisons"]["module.py:2:4"] = [[0, 99, False, False]]
        with self.assertRaises(ValueError):
            merge_profiles([changed])


if __name__ == "__main__":
    unittest.main()
