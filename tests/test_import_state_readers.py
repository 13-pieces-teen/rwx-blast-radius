import hashlib
from types import SimpleNamespace
import unittest

from import_state_readers import StateReaders, build_name_profile


def fixture(sources, lines, attributes=None, class_dict=None, namespace=None, ids=None):
    baseline = {'source_commit': 'tiny', 'ids': ids or ['test0', 'test1', 'test2'],
                'source_hashes': {p: hashlib.sha256(t.encode()).hexdigest() for p, t in sources.items()},
                'lines': lines, 'attributes': attributes or {}, 'symbols': {'sqlglot/m.py:Base': '7'}}
    names = build_name_profile(sources, baseline)
    selector = SimpleNamespace(map=baseline, class_dict_readers=class_dict or {}, namespace_profile=namespace)
    return StateReaders(selector, names)


def cls(symbol, mro, own):
    path, qual = symbol.rsplit(':', 1)
    return {'path': path, 'qualname': qual, 'class_name': qual.rsplit('.', 1)[-1], 'mro': mro, 'own_fields': own}


class ImportStateReadersTests(unittest.TestCase):
    def test_global_read_uses_executed_name_line(self):
        readers = fixture({'sqlglot/m.py': 'FLAG=True\ndef f():\n    return FLAG\n'}, {'sqlglot/m.py': {'3': '1'}})
        bits, _ = readers.readers(['sqlglot/m.py::FLAG'])
        self.assertEqual(bits, 1)

    def test_shadowed_local_is_not_a_module_global(self):
        readers = fixture({'sqlglot/m.py': 'FLAG=True\ndef f():\n    FLAG=False\n    return FLAG\n'}, {'sqlglot/m.py': {'4': '1'}})
        self.assertEqual(readers.impact(['sqlglot/m.py::FLAG'])[0], 0)

    def test_test_collection_cached_class_value_has_downstream_reader(self):
        sources = {'sqlglot/m.py': 'class Base:\n    FLAG=True\n',
                   'tests/test_a.py': 'from sqlglot.m import Base\nCACHED=Base.FLAG\ndef test_a():\n    assert CACHED\n'}
        readers = fixture(sources, {'tests/test_a.py': {'4': '4'}}, {'sqlglot/m.py:Base:FLAG': '1'})
        bits, details = readers.impact(['sqlglot/m.py:Base:FLAG'])
        self.assertEqual(bits, 5)
        self.assertIn('tests/test_a.py::CACHED', [r['key'] for r in details['reader_fields']])

    def test_inherited_owner_readers_include_derived_namespace_but_not_override(self):
        sources = {'sqlglot/m.py': 'class Base:\n    FLAG=True\nclass Child(Base): pass\nclass Override(Base):\n    FLAG=False\n'}
        base='sqlglot/m.py:Base';child='sqlglot/m.py:Child';override='sqlglot/m.py:Override'
        namespace = {'all': {'readers': {child:'2',override:'4'}}, 'opaque': {'readers': {child:'2',override:'4'}}, 'fields': {'readers': {}}, 'keys': {'readers': {}}}
        readers=fixture(sources,{}, {base+':FLAG':'1',override+':FLAG':'4'},namespace=namespace)
        snapshot={'fields':{base+':FLAG':{'defined_on':base}},'classes':{base:cls(base,[base],['FLAG']),child:cls(child,[child,base],[]),override:cls(override,[override,base],['FLAG'])}}
        bits,_=readers.impact([base+':FLAG'],snapshot,snapshot)
        self.assertEqual(bits,3)

    def test_alias_groups_are_lists_and_transitive(self):
        sources={'sqlglot/m.py':'A=[]\nB=A\ndef f():\n    return B\n'}
        readers=fixture(sources,{'sqlglot/m.py':{'4':'2'}})
        snapshot={'aliases':[['sqlglot/m.py::A','sqlglot/m.py::B']]}
        self.assertEqual(readers.impact(['sqlglot/m.py::A'],snapshot,snapshot)[0],2)

    def test_namespace_key_readers_only_apply_to_added_or_removed_fields(self):
        key='sqlglot/m.py:Base:FLAG';symbol='sqlglot/m.py:Base'
        namespace={'all':{'readers':{symbol:'2'}},'opaque':{'readers':{}},'fields':{'readers':{}},'keys':{'readers':{symbol:'2'}}}
        readers=fixture({'sqlglot/m.py':'class Base:\n    FLAG=True\n'},{},{key:'1'},namespace=namespace)
        self.assertEqual(readers.impact([{'key':key,'change':'value'}])[0],1)
        self.assertEqual(readers.impact([{'key':key,'change':'added'}])[0],3)

    def test_global_read_inside_comprehension(self):
        readers=fixture({'sqlglot/m.py':'FLAG=True\ndef f():\n    return [FLAG for _ in range(2)]\n'},{'sqlglot/m.py':{'3':'1'}})
        self.assertEqual(readers.impact(['sqlglot/m.py::FLAG'])[0],1)

    def test_unrelated_classes_with_same_field_do_not_share_fallback_readers(self):
        sources={'sqlglot/m.py':'class A:\n    required_args={"x"}\nclass B:\n    required_args={"y"}\ndef use(value):\n    return value.required_args\n'}
        readers=fixture(sources,{'sqlglot/m.py':{'6':'3'}})
        readers.map['symbols']={'sqlglot/m.py:A':'1','sqlglot/m.py:B':'2'}
        self.assertEqual(readers.impact(['sqlglot/m.py:A:required_args'])[0],1)
        self.assertEqual(readers.impact(['sqlglot/m.py:B:required_args'])[0],2)

    def test_unused_receiver_does_not_pick_other_class_same_name_reads(self):
        sources={'sqlglot/m.py':'class A:\n    arg_types={"x":True}\nclass B:\n    arg_types={"y":True}\ndef use(value):\n    return value.arg_types\n'}
        readers=fixture(sources,{'sqlglot/m.py':{'6':'2'}})
        readers.map['symbols']={'sqlglot/m.py:B':'2'}
        self.assertEqual(readers.impact(['sqlglot/m.py:A:arg_types'])[0],0)
        self.assertEqual(readers.impact(['sqlglot/m.py:B:arg_types'])[0],2)

    def test_receiver_bound_preserves_cached_test_class_field(self):
        sources={'sqlglot/m.py':'class Base:\n    FLAG=True\n',
                 'tests/test_a.py':'from sqlglot.m import Base\nclass Case:\n    CACHED=Base.FLAG\n    def test_a(self):\n        assert self.CACHED\n'}
        readers=fixture(sources,{'tests/test_a.py':{'5':'1'}},ids=['tests/test_a.py::Case::test_a'])
        self.assertEqual(readers.impact(['sqlglot/m.py:Base:FLAG'])[0],1)

    def test_default_reader_override_keeps_test_declaration_cache(self):
        sources={'sqlglot/m.py':'def f(flag=True):\n    return flag\n',
                 'tests/test_a.py':'from sqlglot.m import f\nCACHED=f.__defaults__[0]\ndef test_a():\n    assert CACHED\n'}
        readers=fixture(sources,{'tests/test_a.py':{'4':'4'}})
        key='sqlglot/m.py::f'
        readers.names['readers'][key]='7'
        bits,details=readers.impact([key],reader_overrides={key:1})
        self.assertEqual(bits,5)
        self.assertIn('tests/test_a.py::CACHED',[r['key'] for r in details['reader_fields']])

    def test_default_reader_override_follows_inherited_owner(self):
        sources={'sqlglot/m.py':'class Base:\n    def f(self,flag=True):\n        return flag\nclass Child(Base): pass\n'}
        base='sqlglot/m.py:Base';child='sqlglot/m.py:Child';key=base+':f'
        readers=fixture(sources,{}, {key:'7',child+':f':'7'})
        snapshot={'fields':{key:{'defined_on':base}},'classes':{
            base:cls(base,[base],['f']),child:cls(child,[child,base],[])}}
        self.assertEqual(readers.impact([key],snapshot,snapshot,reader_overrides={key:1})[0],1)

    def test_override_does_not_remove_other_derived_value(self):
        sources={'sqlglot/m.py':'def f(flag=True):\n    return flag\nCACHED=f()\ndef use():\n    return CACHED\n'}
        readers=fixture(sources,{'sqlglot/m.py':{'5':'4'}})
        self.assertEqual(readers.impact(['sqlglot/m.py::f','sqlglot/m.py::CACHED'],
                                       reader_overrides={'sqlglot/m.py::f':1})[0],5)


if __name__ == '__main__':
    unittest.main()
