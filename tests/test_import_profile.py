import copy
import unittest

from tools.merge_import_profile import merge_import_profile


class ImportProfileTests(unittest.TestCase):
    def fixtures(self):
        base = {"source_commit": "pin", "ids": ["test"], "source_hashes": {"a.py": "sha"},
                "lines": {"a.py": {"1": "1"}}, "attributes": {"A:x": "1"},
                "collection_lines": {"a.py": [1]}, "collection_attributes": ["A:x"],
                "collection_reads": ["old.fixture"], "collection_lambda_bodies": {"a.py": [2]}}
        profile = {"source_commit": "pin", "ids": ["test"], "source_hashes": {"a.py": "sha"},
                   "complete": True, "failures": [], "collection_lines": {"a.py": [3]},
                   "collection_attributes": ["B:y"], "collection_reads": ["new.fixture"],
                   "lambda_bodies": {"a.py": [4]}, "collection_class_dict": ["B"]}
        return base, profile

    def test_preserves_existing_edges_and_adds_import_state(self):
        base, profile = self.fixtures()
        original = copy.deepcopy(base)
        header = {"source_commit": "pin", "ids": ["test"], "complete": True, "failures": []}
        collection = {**header, "lambda_bodies": {"a.py": [5]}}
        dictionaries = {**header, "readers": {"A": "1"}, "collection": ["A"]}
        result, calls, classes = merge_import_profile(base, profile, collection, dictionaries)
        self.assertEqual(result["lines"], base["lines"])
        self.assertEqual(result["attributes"], base["attributes"])
        self.assertEqual(result["collection_lines"]["a.py"], [1, 3])
        self.assertEqual(result["collection_attributes"], ["A:x", "B:y"])
        self.assertEqual(set(result["collection_reads"]), {"old.fixture", "new.fixture"})
        self.assertEqual(calls["lambda_bodies"]["a.py"], [2, 4, 5])
        self.assertEqual(classes["collection"], ["A", "B"])
        self.assertEqual(classes["readers"], {"A": "1"})
        self.assertEqual(base, original)

    def test_rejects_untrustworthy_supplements(self):
        for field, value in [("complete", False), ("failures", ["import failed"]),
                             ("ids", ["other"]), ("source_commit", "other"),
                             ("source_hashes", {"a.py": "different"})]:
            base, profile = self.fixtures()
            profile[field] = value
            with self.assertRaises(ValueError):
                merge_import_profile(base, profile)


if __name__ == "__main__":
    unittest.main()
