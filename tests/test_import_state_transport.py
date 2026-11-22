import copy
import json
import unittest

from tools.import_state import diff
from tools.import_state_probe import pack_snapshot, unpack_snapshot


class StateTransportTests(unittest.TestCase):
    def baseline(self):
        return {'schema_version': 1,
                'fields': {'a': {'digest': 'one', 'kind': 'module'},
                           'b': {'digest': 'two', 'kind': 'class', 'defined_on': 'X'}},
                'classes': {'X': {'mro': ['X'], 'own_fields': ['b']}},
                'aliases': [], 'unsupported_fields': {},
                'stats': {'count': 2, 'seconds': 1}, 'normalization_policy': {}}

    def check_roundtrip(self, before, after):
        frozen = copy.deepcopy(before)
        packet = json.loads(json.dumps(pack_snapshot(before, after)))
        restored = unpack_snapshot(before, packet)
        self.assertEqual(restored, after)
        self.assertEqual(packet['changed'], diff(before, after)['changed'])
        self.assertEqual(before, frozen)
        return packet

    def test_no_change_omits_full_fields_and_metadata(self):
        before = self.baseline()
        packet = self.check_roundtrip(before, copy.deepcopy(before))
        self.assertEqual(packet['fields'], {})
        self.assertEqual(packet['top'], {})

    def test_value_add_remove_alias_and_class_changes_are_lossless(self):
        before = self.baseline()
        after = copy.deepcopy(before)
        del after['fields']['a']
        after['fields']['b']['digest'] = 'changed'
        after['fields']['c'] = {'digest': 'three', 'kind': 'module'}
        after['classes']['Y'] = {'mro': ['Y', 'X'], 'own_fields': []}
        after['aliases'] = [['b', 'c']]
        after['unsupported_fields'] = {'c': ['some.Type']}
        after['stats']['seconds'] = 2
        self.check_roundtrip(before, after)

    def test_digest_equal_metadata_changes_and_top_deletions_survive(self):
        before = self.baseline()
        after = copy.deepcopy(before)
        after['fields']['b']['name'] = 'renamed metadata'
        del after['normalization_policy']
        after['extra'] = {'new': True}
        after['nullable'] = None
        self.check_roundtrip(before, after)


if __name__ == '__main__':
    unittest.main()
