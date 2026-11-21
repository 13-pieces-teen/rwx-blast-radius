import unittest

from tools.merge_lambda_profiles import merge_profiles


class LambdaMergeTests(unittest.TestCase):
    def profiles(self):
        header = {"complete": True, "failures": [], "source_commit": "pin", "ids": ["a", "b"],
                  "source_hashes": {"m.py": "sha"}, "collection": []}
        return [{**header, "mode": "warm", "readers": {"m.py:1:0": "1", "m.py:2:0": "0"}},
                {**header, "mode": "cold", "readers": {"m.py:1:0": "2", "m.py:2:0": "0"}},
                {**header, "mode": "eager-import-collection", "readers": {"m.py:1:0": "0", "m.py:2:0": "0"},
                 "collection": ["m.py:2:0"]}]

    def test_unions_calls_and_retains_uncalled_catalog(self):
        profiles = self.profiles()
        result = merge_profiles(profiles)
        self.assertEqual(result["readers"], {"m.py:1:0": "3", "m.py:2:0": "0"})
        self.assertEqual(result["collection"], ["m.py:2:0"])
        self.assertEqual(profiles, self.profiles())

    def test_requires_complete_same_baseline_observations(self):
        with self.assertRaises(ValueError):
            merge_profiles(self.profiles()[:2])
        for key, value in [("complete", False), ("failures", ["failed"]), ("ids", ["other"]),
                           ("source_commit", "other"), ("source_hashes", {}), ("readers", {})]:
            profiles = self.profiles()
            profiles[1][key] = value
            with self.assertRaises(ValueError):
                merge_profiles(profiles)


if __name__ == "__main__":
    unittest.main()
