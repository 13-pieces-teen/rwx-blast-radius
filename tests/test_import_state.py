import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
import json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.import_state import _Normalizer, diff


class ImportStateTests(unittest.TestCase):
    def capture(self, source, overrides=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sqlglot').mkdir()
            (root / 'sqlglot/__init__.py').write_text(source)
            command = "import json,sys;from tools.import_state import snapshot;r=json.load(sys.stdin);print(json.dumps(snapshot(**r)))"
            result = subprocess.run([sys.executable, '-B', '-c', command], cwd=ROOT,
                input=json.dumps({'source': str(root), 'overrides': overrides or {}, 'imports': ['sqlglot/__init__.py']}),
                capture_output=True, text=True, check=True)
            return json.loads(result.stdout)

    def test_literal_derived_and_inherited_metadata(self):
        code = 'class A:\n X=1\n Y=X+1\nclass B(A):\n pass\nALIAS=A\nVALUE=A.X\n'
        before = self.capture(code)
        after = self.capture(code, {'sqlglot/__init__.py': code.replace('X=1', 'X=2')})
        changed = {r['key'] for r in diff(before, after)['changed']}
        self.assertIn('sqlglot/__init__.py:A:X', changed)
        self.assertIn('sqlglot/__init__.py:A:Y', changed)
        self.assertIn('sqlglot/__init__.py::VALUE', changed)
        self.assertEqual(after['classes']['sqlglot/__init__.py:B']['mro'][:2],
                         ['sqlglot/__init__.py:B', 'sqlglot/__init__.py:A'])

    def test_function_body_vs_dispatch_lambda_and_defaults(self):
        code = 'def f(x=1):\n return x+1\nD={"f": lambda x: x+1}\n'
        before = self.capture(code)
        changed_body = self.capture(code, {'sqlglot/__init__.py': code.replace('return x+1', 'return x+2')})
        self.assertEqual(before['fields']['sqlglot/__init__.py::f']['digest'], changed_body['fields']['sqlglot/__init__.py::f']['digest'])
        changed_lambda = self.capture(code, {'sqlglot/__init__.py': code.replace('lambda x: x+1', 'lambda x: x+2')})
        self.assertNotEqual(before['fields']['sqlglot/__init__.py::D']['digest'], changed_lambda['fields']['sqlglot/__init__.py::D']['digest'])
        changed_default = self.capture(code, {'sqlglot/__init__.py': code.replace('x=1', 'x=2')})
        self.assertNotEqual(before['fields']['sqlglot/__init__.py::f']['digest'], changed_default['fields']['sqlglot/__init__.py::f']['digest'])

    def test_equal_value_alias_split_changes_digest(self):
        code = 'A=[]\nB=A\n'
        before = self.capture(code)
        after = self.capture(code, {'sqlglot/__init__.py': 'A=[]\nB=[]\n'})
        self.assertEqual({r['key'] for r in diff(before, after)['changed']},
                         {'sqlglot/__init__.py::A', 'sqlglot/__init__.py::B'})

    def test_class_keyed_maps_stable_but_string_dict_order_retained(self):
        class A: pass
        class B: pass
        norm = _Normalizer()
        self.assertEqual(norm.digest({A: 1, B: 2}), norm.digest({B: 2, A: 1}))
        self.assertNotEqual(norm.digest({'a': 1, 'b': 2}), norm.digest({'b': 2, 'a': 1}))

    def test_nested_alias_relation_is_observed(self):
        x = []
        norm = _Normalizer()
        self.assertNotEqual(norm.digest([x, x]), norm.digest([[], []]))

    def test_generated_closure_and_bound_method(self):
        code = 'def factory(x):\n def f():\n  return x\n return f\nclass C:\n @classmethod\n def f(cls):\n  return 1\nD={"closure":factory(1),"bound":C.f}\n'
        before = self.capture(code)
        after = self.capture(code, {'sqlglot/__init__.py': code.replace('factory(1)', 'factory(2)')})
        self.assertNotEqual(before['fields']['sqlglot/__init__.py::D']['digest'], after['fields']['sqlglot/__init__.py::D']['digest'])
        self.assertFalse(before['unsupported_fields'])

    def test_slot_instances_and_regex_are_stable(self):
        code = 'import re\nclass C:\n __slots__=("x",)\n def __init__(self):\n  self.x=3\nITEM=C()\nRX=re.compile("abc",re.I)\n'
        before, after = self.capture(code), self.capture(code)
        self.assertEqual(before['fields'], after['fields'])
        self.assertFalse(before['unsupported_fields'])

    def test_type_set_policy_keeps_members_and_ordered_tuples(self):
        code = 'class A: pass\nclass B: pass\nTYPES=tuple({A,B})\nORDER=(A,B)\ndef f(x):\n return isinstance(x,TYPES)\n'
        before = self.capture(code)
        self.assertEqual(before['normalization_policy']['unordered_type_tuple_fields'], ['sqlglot/__init__.py::TYPES'])
        changed = self.capture(code, {'sqlglot/__init__.py': code.replace('tuple({A,B})', 'tuple({A})')})
        self.assertNotEqual(before['fields']['sqlglot/__init__.py::TYPES']['digest'], changed['fields']['sqlglot/__init__.py::TYPES']['digest'])
        changed_order = self.capture(code, {'sqlglot/__init__.py': code.replace('ORDER=(A,B)', 'ORDER=(B,A)')})
        self.assertNotEqual(before['fields']['sqlglot/__init__.py::ORDER']['digest'], changed_order['fields']['sqlglot/__init__.py::ORDER']['digest'])
        iteration = self.capture(code + 'LIST=list(TYPES)\n')
        self.assertEqual(iteration['normalization_policy']['unordered_type_tuple_fields'], [])


if __name__ == '__main__':
    unittest.main()
