"""Actual tiny baseline/mutant executions plus state-reader composition."""
import ast
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from default_value_selector import match_default_value_edit, plan_default_value
from import_state_readers import StateReaders
from test_default_profile_execution import execute


class ToySelector:
    def __init__(self, folder, original, observed, observer, count):
        self.source = folder
        self.all_bits = (1 << count) - 1
        self.cache = {}
        self.map = {'ids': [f'tests/toy.py::C::test_{i}' for i in range(count)],
                    'source_hashes': {'module.py': hashlib.sha256(original.encode()).hexdigest()},
                    'collection_lines': {'module.py': [line for line, contexts in observed.items() if '@collection' in contexts]}}
        def bits(contexts):
            return sum(1 << i for i in range(count) if i in contexts or '@class:C' in contexts)
        self.coverage = {line: bits(contexts) for line, contexts in observed.items()}
        functions = {}
        for site, definition in observer.known.items():
            record = observer.records[site]
            parameters = {}
            for name, slot in definition['parameters'].items():
                values = record['parameters'][name]
                parameters[name] = {'kind': slot['kind'], 'default': slot['default'],
                    **{key: format(bits(values[key]), 'x') for key in ('seen', 'possible', 'opaque')},
                    'collection_possible': '@collection' in values['possible'],
                    'collection_opaque': '@collection' in values['opaque']}
            functions[site] = {'metadata_readers': format(bits(record['metadata']), 'x'),
                'metadata_collection': '@collection' in record['metadata'],
                'metadata_fixture': '@class:C' in record['metadata'], 'identity_opaque': record['opaque'],
                'parameters': parameters, 'state_effect': 'opaque'}
        self.default_dependencies = {'functions': functions, 'persistent_opaque_tests': format(self.all_bits, 'x')}
        self.class_dict_readers = {}
        self.namespace_profile = None

    def lines(self, path, first, last):
        bits = 0
        if path == 'module.py':
            for line in range(first, last + 1):
                bits |= self.coverage.get(line, 0)
        return bits

    def load_source(self, path):
        if path not in self.cache:
            text = (self.source / path).read_text()
            tree = ast.parse(text)
            self.cache[path] = (text, tree, [n for n in ast.walk(tree) if hasattr(n, 'lineno')])
        return self.cache[path]


class DefaultValueSelectorTests(unittest.TestCase):
    def run_observed(self, original, mutant, actions, fixture=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        path = folder / 'module.py'
        path.write_text(original)
        observed, old_failures, observer = execute(original, path, actions, fixture, True)
        _, actual_failures, _ = execute(mutant, path, actions, fixture, False)
        self.assertEqual(old_failures, [])
        selector = ToySelector(folder, original, observed, observer, len(actions))
        plan = plan_default_value(selector, 'module.py', ast.parse(original), ast.parse(mutant))
        self.assertIsNotNone(plan)
        return plan, selector, actual_failures

    def test_actual_opposite_bool_excluded_despite_global_opaque_guard(self):
        plan, _, failures = self.run_observed('def f(x=True):\n return x\n', 'def f(x=False):\n return x\n',
            ['assert m.f(False) is False', 'assert m.f() is True', 'assert m.f(True) is True'])
        self.assertEqual(failures, [1])
        self.assertEqual(plan.body_bits, 7)
        self.assertEqual(plan.bits, 6)  # Explicit old value remains possibly implicit.
        self.assertTrue(plan.can_skip_snapshot)

    def test_actual_alias_metadata_is_retained(self):
        plan, _, failures = self.run_observed('def f(x=True):\n return x\nalias=f\n',
            'def f(x=False):\n return x\nalias=f\n', ['assert m.f(False) is False', 'assert m.alias.__defaults__[0] is True'])
        self.assertEqual(failures, [1])
        self.assertEqual(plan.runtime_bits, 0)
        self.assertEqual(plan.metadata_bits, 2)
        self.assertEqual(plan.bits, 2)

    def test_fixture_default_use_expands_the_whole_class(self):
        plan, _, failures = self.run_observed('def f(x=True):\n return x\n', 'def f(x=False):\n return x\n',
            ['assert m.CACHED is True', 'assert m.CACHED is True'], 'm.CACHED=m.f()')
        self.assertEqual(failures, [0, 1])
        self.assertEqual(plan.bits, 3)

    def test_import_derived_value_remains_a_changed_state_dependency(self):
        original = 'def f(x=True):\n return x\nCACHED=f()\n'
        plan, _, failures = self.run_observed(original, original.replace('x=True', 'x=False'),
                                            ['assert m.CACHED is True', 'assert True'])
        self.assertEqual(failures, [0])
        self.assertFalse(plan.can_skip_snapshot)
        before = {'fields': {'module.py::f': {'value_kind': 'builtins.function'},
                             'module.py::CACHED': {'value_kind': 'builtins.bool'}}, 'aliases': []}
        overrides = plan.reader_overrides(before, before)
        self.assertIn('module.py::f', overrides)
        self.assertNotIn('module.py::CACHED', overrides)

    def test_int_none_string_defaults_use_actual_body_coverage(self):
        for before, after, assertion in [('3', '-1', '==3'), ('None', '1', 'is None'), ('"old"', '"new"', '=="old"')]:
            with self.subTest(default=before):
                plan, _, failures = self.run_observed(f'def f(x={before}):\n return x\n',
                    f'def f(x={after}):\n return x\n', [f'assert m.f() {assertion}', 'assert True'])
                self.assertEqual(failures, [0])
                self.assertEqual(plan.bits, 1)
                self.assertFalse(plan.can_skip_snapshot)

    def test_async_generator_nested_and_decorated_syntax_is_supported(self):
        for code in ['async def f(x=1):\n return x\n', 'def f(x=1):\n yield x\n',
                     'def outer():\n def f(x=1):\n  return x\n return f\n',
                     '@decorate\ndef f(x=1):\n return x\n']:
            with self.subTest(code=code):
                edit = match_default_value_edit(ast.parse(code), ast.parse(code.replace('x=1', 'x=2')))
                self.assertIsNotNone(edit)

    def test_actual_deferred_and_decorated_calls_keep_body_failures(self):
        cases = [
            ('async def f(x=1):\n return x\n', 'coroutine=m.f()\ntry:\n coroutine.send(None)\nexcept StopIteration as done:\n assert done.value == 1'),
            ('def f(x=1):\n yield x\n', 'assert list(m.f()) == [1]'),
            ('def outer():\n def f(x=1):\n  return x\n return f\n', 'assert m.outer()() == 1'),
            ('def decorate(fn):\n def wrapper():\n  return fn()\n return wrapper\n@decorate\ndef f(x=1):\n return x\n', 'assert m.f() == 1'),
        ]
        for code, action in cases:
            with self.subTest(code=code):
                plan, _, failures = self.run_observed(code, code.replace('x=1', 'x=2'), [action, 'assert True'])
                self.assertEqual(failures, [0])
                self.assertEqual(plan.bits, 1)
                self.assertFalse(plan.can_skip_snapshot)
                if plan.edit.nested or not plan.edit.simple_descriptor:
                    snapshot = {'fields': {'module.py::f': {'value_kind': 'builtins.function'}}, 'aliases': []}
                    self.assertEqual(plan.reader_overrides(snapshot, snapshot), {})

    def test_nonbool_metadata_fallback_uses_observed_test_lines(self):
        import types
        code = 'def f(x=3):\n return x\n'
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); (folder / 'module.py').write_text(code)
            test_path = folder / 'tests/toy.py'; test_path.parent.mkdir()
            test_source = 'def check(m):\n assert m.f.__defaults__[0] == 3\n'
            test_path.write_text(test_source)
            module = types.ModuleType('module'); exec(compile(code, str(folder / 'module.py'), 'exec'), module.__dict__)
            env = {}; exec(compile(test_source, str(test_path), 'exec'), env)
            reached = set()
            def trace(frame, event, arg):
                if event == 'line' and frame.f_code.co_filename == str(test_path): reached.add(frame.f_lineno)
                return trace
            previous = sys.gettrace(); sys.settrace(trace)
            try: env['check'](module)
            finally: sys.settrace(previous)
            mutant = code.replace('x=3', 'x=4'); exec(compile(mutant, str(folder / 'module.py'), 'exec'), module.__dict__)
            with self.assertRaises(AssertionError): env['check'](module)
            selector = SimpleNamespace(source=folder, all_bits=3, default_dependencies=None, cache={},
                map={'ids':['tests/toy.py::check','tests/other.py::check'], 'source_hashes':{'module.py':'x','tests/toy.py':'x'}, 'collection_lines':{}})
            def load_source(path):
                text=(folder/path).read_text(); tree=ast.parse(text)
                return text,tree,[n for n in ast.walk(tree) if hasattr(n,'lineno')]
            selector.load_source=load_source
            selector.lines=lambda path,first,last: 1 if path=='tests/toy.py' and any(first<=line<=last for line in reached) else 0
            plan=plan_default_value(selector,'module.py',ast.parse(code),ast.parse(mutant))
            self.assertEqual(plan.body_bits,0)
            self.assertEqual(plan.metadata_bits,1)

    def test_only_default_replacement_is_accepted_and_bool_int_differ(self):
        before = ast.parse('def f(x=True):\n return True\n')
        self.assertIsNotNone(match_default_value_edit(before, ast.parse('def f(x=1):\n return True\n')))
        self.assertIsNone(match_default_value_edit(before, ast.parse('def f(x=False):\n return 1\n')))
        self.assertIsNone(match_default_value_edit(before, ast.parse('def f(x=call()):\n return True\n')))

    def test_test_collection_cache_and_alias_edges_survive_reader_override(self):
        original = 'def f(x=True):\n return x\nalias=f\n'
        plan, selector, _ = self.run_observed(original, original.replace('x=True', 'x=False'),
                                             ['assert m.f(False) is False', 'assert True'])
        selector.map.update(attributes={})
        reader = object.__new__(StateReaders)
        reader.selector, reader.map = selector, selector.map
        reader.names = {'readers': {'tests/toy.py::CACHED': '2'}, 'module_namespaces': {}, 'attribute_names': {},
                        'test_declaration_edges': {'module.py::alias': ['tests/toy.py::CACHED']}}
        fields = {key: {'kind': 'module', 'path': 'module.py', 'name': name, 'value_kind': 'builtins.function'}
                  for name, key in [('f', 'module.py::f'), ('alias', 'module.py::alias')]}
        snapshot = {'fields': fields, 'classes': {}, 'aliases': [['module.py::f', 'module.py::alias']]}
        plan.metadata_collection = True
        overrides = plan.reader_overrides(snapshot, snapshot, reader)
        self.assertEqual(set(overrides), {'module.py::f', 'module.py::alias'})
        self.assertEqual(set(overrides.values()), {0})
        bits, _ = reader.readers(['module.py::f'], snapshot, snapshot, reader_overrides=overrides)
        self.assertEqual(bits, 2)
        reader.names['test_declaration_edges'] = {}
        self.assertEqual(set(plan.reader_overrides(snapshot, snapshot, reader).values()), {3})


if __name__ == '__main__':
    unittest.main()
