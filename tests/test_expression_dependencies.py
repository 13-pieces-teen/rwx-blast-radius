import ast
import difflib
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from expression_dependencies import ExpressionMixin, build_plan
from selector import Selector, apply_change, parse_patch


PATH = 'sqlglot/example.py'


def patch(old, new):
    return ''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                     'a/' + PATH, 'b/' + PATH))


def plan(old, new, collection=()):
    modified, touched, insertions = apply_change(old, parse_patch(patch(old, new))[0])
    return build_plan(PATH, old, modified, ast.parse(old), touched, insertions,
                      Selector.code_objects(old, PATH), collection)


class Refined(ExpressionMixin, Selector):
    pass


class ExpressionTests(unittest.TestCase):
    def selection(self, old, new, arguments, collection=(), reads=0):
        namespace = {}
        exec(compile(old, PATH, 'exec'), namespace)
        lines = {}
        for index, args in enumerate(arguments):
            def trace(frame, event, arg):
                if event == 'line' and frame.f_code.co_filename == PATH:
                    lines[frame.f_lineno] = lines.get(frame.f_lineno, 0) | (1 << index)
                return trace
            previous = sys.gettrace()
            sys.settrace(trace)
            try:
                namespace['f'](*args)
            finally:
                sys.settrace(previous)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / PATH).parent.mkdir(parents=True)
            (root / PATH).write_text(old, encoding='utf8')
            metadata = dict(ids=[f'test{i}' for i in range(len(arguments))],
                            source_hashes={PATH: hashlib.sha256(old.encode()).hexdigest()},
                            lines={PATH: {str(k): format(v, 'x') for k, v in lines.items()}},
                            collection_lines={PATH: list(collection)}, reads={PATH: format(reads, 'x')},
                            modules={}, symbols={})
            maps = root / 'maps.gz'
            maps.write_bytes(gzip.compress(json.dumps(metadata).encode()))
            tests = [dict(id=x, number=i, seconds=.01) for i, x in enumerate(metadata['ids'])]
            base = Selector(root, maps, tests)
            candidate = Refined(root, maps, tests)
            baseline = {test['number'] for test in base.select(patch(old, new))}
            selected = {test['number'] for test in candidate.select(patch(old, new))}
            return baseline, selected, candidate.expression_last

    def test_short_circuit_operand_uses_actual_trace(self):
        old = 'def f(a, b):\n    return (\n        a\n        and b == 1\n    )\n'
        base, selected, details = self.selection(old, old.replace('==', '!='), [(False, 1), (True, 1)])
        self.assertEqual(base, {0, 1})
        self.assertEqual(selected, {1})
        self.assertEqual(details['anchors'], [4])

    def test_multiline_comparison_anchors_first_operand_not_operator_line(self):
        old = 'def f(a,b):\n    return a and (\n        b\n        == 1\n    )\n'
        p = plan(old, old.replace('==', '!='))
        self.assertEqual(p.anchor_lines, {3})
        _, selected, _ = self.selection(old, old.replace('==', '!='), [(False, 1), (True, 1)])
        self.assertEqual(selected, {1})

    def test_two_operations_on_one_line_keep_both_readers(self):
        old = 'def f(a,b):\n    return a and b == 1\n'
        _, selected, _ = self.selection(old, old.replace('==', '!='), [(False, 1), (True, 1)])
        self.assertEqual(selected, {0, 1})

    def test_changed_constant_keeps_only_its_execution_line(self):
        old = 'def f(a,b):\n    return (a and\n        b == 1)\n'
        _, selected, details = self.selection(old, old.replace('1)', '2)'), [(False, 1), (True, 1)])
        self.assertEqual(selected, {1})
        self.assertEqual(details['kind'], 'Constant')

    def test_short_circuit_operator_changes_select_first_operand(self):
        old = 'def f(a,b):\n    return (\n        a\n        and b\n    )\n'
        _, selected, details = self.selection(old, old.replace('and', 'or'), [(False, 1), (True, 1)])
        self.assertEqual(selected, {0, 1})
        self.assertEqual(details['anchors'], [3])

    def test_conditional_expression_branch_uses_its_own_trace(self):
        old = 'def f(a,b):\n    return (\n        b + 1\n        if a else 0\n    )\n'
        _, selected, _ = self.selection(old, old.replace('+ 1', '+ 2'), [(False, 1), (True, 1)])
        self.assertEqual(selected, {1})

    def test_compiler_folded_operand_without_load_falls_back(self):
        old = 'def f(a):\n    return a and (1 + 2)\n'
        self.assertIsNone(plan(old, old.replace('1 + 2', '1 + 3')))

    def test_constant_dead_branch_has_no_anchor(self):
        old = 'def f(a):\n    return False and (\n        a == 1\n    )\n'
        self.assertIsNone(plan(old, old.replace('==', '!=')))

    def test_collection_execution_requires_state_handling(self):
        old = 'def f(a,b):\n    return (a and\n        b == 1)\n'
        _, selected, details = self.selection(old, old.replace('==', '!='), [(False, 1), (True, 1)], collection=[3])
        self.assertEqual(selected, {0, 1})
        self.assertTrue(details['collection_touched'])

    def test_unrelated_collection_condition_prefix_does_not_force_all(self):
        old = 'def f(a,b):\n    return (a and\n        b == 1)\n'
        _, selected, details = self.selection(old, old.replace('==', '!='), [(False, 1), (True, 1)], collection=[2])
        self.assertEqual(selected, {1})
        self.assertFalse(details['collection_touched'])

    def test_source_reader_union_remains(self):
        old = 'def f(a,b):\n    return (a and\n        b == 1)\n'
        _, selected, _ = self.selection(old, old.replace('==', '!='), [(False, 1), (True, 1)], reads=1)
        self.assertEqual(selected, {0, 1})

    def test_default_and_decorator_not_runtime_expression(self):
        for old in ['def f(a=True):\n    return a\n', '@deco(True)\ndef f(a):\n    return a\n']:
            with self.subTest(old=old):
                self.assertIsNone(plan(old, old.replace('True', 'False')))

    def test_deferred_scopes_are_not_confused_with_enclosing_creation(self):
        for expr in ['lambda x: x == 1', '[x == 1 for x in a]', '(x == 1 for x in a)']:
            old = f'def f(a):\n    return {expr}\n'
            with self.subTest(expr=expr):
                self.assertIsNone(plan(old, old.replace('==', '!=')))

    def test_two_separate_statements_are_not_one_expression(self):
        old = 'def f(a):\n    x = a + 1\n    return x + 2\n'
        self.assertIsNone(plan(old, old.replace('+ 1', '+ 3').replace('+ 2', '+ 4')))

    def test_statement_shape_or_line_relocation_rejects(self):
        old = 'def f(a):\n    x = a + 1\n    return x\n'
        self.assertIsNone(plan(old, old.replace('    x = a + 1\n', '    if a:\n        x = a + 1\n')))

    def test_while_short_circuit_with_repeated_evaluation(self):
        old = 'def f(a,b):\n    while (a and\n           b < 1):\n        b += 1\n    return b\n'
        _, selected, _ = self.selection(old, old.replace('b < 1', 'b > 1'), [(False, 0), (True, 0)])
        self.assertEqual(selected, {1})


if __name__ == '__main__':
    unittest.main()
