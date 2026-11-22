"""Refine runtime edges to the evaluated expression changed by an AST edit.

This uses only the pinned source, its bytecode locations, and baseline line
coverage. It is not a proof of cross-test state isolation. Import-time effects
remain the responsibility of ImportStateMixin; source/reflection/scope guards
remain in Selector. No mutation labels or test identities enter this module.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
import dis

from selector import apply_change, same_ast


DEFERRED = (ast.Lambda, ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)
BINDING = (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await)


@dataclass(frozen=True)
class ExpressionPlan:
    path: str
    touched: frozenset[int]
    anchor_lines: frozenset[int]
    expression_span: tuple[int, int, int, int]
    expression_kind: str
    collection_touched: bool


def position(node):
    return (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)


def changed_expression(before, after):
    """Smallest aligned expression containing every semantic AST difference.

    A line diff cannot distinguish two operators on one line. AST alignment
    does, but the eventual line dependency deliberately remains their union.
    List insertion/deletion outside an expression and statement edits reject.
    """
    if same_ast(before, after):
        return None
    if type(before) is not type(after):
        return (before, after) if isinstance(before, ast.expr) and isinstance(after, ast.expr) else None
    if not isinstance(before, ast.AST):
        return None
    changed = [(getattr(before, f), getattr(after, f)) for f in before._fields
               if not same_ast(getattr(before, f), getattr(after, f))]
    if len(changed) == 1:
        left, right = changed[0]
        if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
            pairs = [(a, b) for a, b in zip(left, right) if not same_ast(a, b)]
            if len(pairs) == 1:
                left, right = pairs[0]
        child = changed_expression(left, right)
        if child is not None:
            return child
    return (before, after) if isinstance(before, ast.expr) else None


def first_operand(node):
    """An operand necessarily evaluated whenever this expression is entered.

    Requiring a real load for this operand handles multiline operations and
    missing line events. Constant-folded/dead operands without such a load do
    not justify narrowing and are left to the ordinary statement dependency.
    """
    if isinstance(node, (ast.Name, ast.Constant)):
        return node
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        return first_operand(node.value)
    if isinstance(node, ast.Call):
        return first_operand(node.func)
    if isinstance(node, (ast.BinOp, ast.Compare)):
        return first_operand(node.left)
    if isinstance(node, ast.UnaryOp):
        return first_operand(node.operand)
    if isinstance(node, ast.BoolOp):
        return first_operand(node.values[0])
    if isinstance(node, ast.IfExp):
        return first_operand(node.test)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and node.elts:
        return first_operand(node.elts[0])
    if isinstance(node, ast.Dict) and node.keys and node.keys[0] is not None:
        return first_operand(node.keys[0])
    return None


def load_anchors(codes):
    """Index real loads once per unchanged source file, not once per patch."""
    found = {}
    for sequence in codes.values():
        for code in sequence:
            intervals = list(code.co_lines())
            interval_index = 0
            for instruction in dis.get_instructions(code):
                if not instruction.opname.startswith('LOAD_'):
                    continue
                p = instruction.positions
                while interval_index < len(intervals) and instruction.offset >= intervals[interval_index][1]:
                    interval_index += 1
                if interval_index < len(intervals):
                    start, end, line = intervals[interval_index]
                    if start <= instruction.offset < end and line is not None:
                        key = (p.lineno, p.col_offset, p.end_lineno, p.end_col_offset)
                        found.setdefault(key, set()).add(line)
    return {key: frozenset(value) for key, value in found.items()}


def locations_unchanged(left, right, old, new):
    if left is old and right is new:
        return True
    if isinstance(left, ast.AST) and isinstance(right, ast.AST):
        return (type(left) is type(right)
                and getattr(left, 'lineno', None) == getattr(right, 'lineno', None)
                and getattr(left, 'end_lineno', None) == getattr(right, 'end_lineno', None)
                and all(locations_unchanged(getattr(left, f), getattr(right, f), old, new)
                        for f in left._fields))
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(locations_unchanged(a, b, old, new) for a, b in zip(left, right))
    return left == right


def build_plan(path, original, modified, tree, touched, insertions, codes, collection, anchors=None):
    if insertions or not touched:
        return None
    after_tree = ast.parse(modified, filename=path)
    pair = changed_expression(tree, after_tree)
    if pair is None:
        return None
    old, new = pair
    # Preserve source-line identity, including unrelated text relocations. A
    # changed column width is harmless: the old compiler provides the anchor.
    if not locations_unchanged(tree, after_tree, old, new):
        return None
    if (old.lineno != new.lineno or old.end_lineno != new.end_lineno
            or not all(old.lineno <= line <= old.end_lineno for line in touched)):
        return None
    if any(isinstance(n, (*DEFERRED, *BINDING)) for root in pair for n in ast.walk(root)):
        return None
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    node = old
    function = None
    while id(node) in parents:
        parent = parents[id(node)]
        if isinstance(parent, (*DEFERRED, *BINDING)):
            return None
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not any(node is body for body in parent.body):
                return None  # signature/default/decorator/annotation
            function = parent
            break
        if isinstance(parent, ast.ClassDef):
            return None
        node = parent
    if function is None:
        return None
    operand = first_operand(old)
    if operand is None:
        return None
    if callable(anchors):
        anchors = anchors()
    anchor_lines = (load_anchors(codes) if anchors is None else anchors).get(position(operand))
    if not anchor_lines:
        return None
    return ExpressionPlan(path, frozenset(touched), anchor_lines, position(old), type(old).__name__,
                          bool(anchor_lines & set(collection)))


class ExpressionMixin:
    """Place before ImportStateMixin and DefaultsMixin in the selector MRO."""
    def __init__(self, *args, precision_expressions=True, **kwargs):
        self.precision_expressions = precision_expressions
        self.expression_plan = None
        self.expression_last = None
        self._expression_load_anchors = {}
        super().__init__(*args, **kwargs)

    def select_change(self, change):
        previous = self.expression_plan
        self.expression_plan = None
        self.expression_last = None
        try:
            if (self.precision_expressions and self.allow_operator_proof
                    and change.old == change.new and change.old.endswith('.py')
                    and change.old in self.map.get('source_hashes', {})):
                try:
                    original, tree, _ = self.load_source(change.old)
                    modified, touched, insertions = apply_change(original, change)
                    def anchors():
                        if change.old not in self.code_cache:
                            self.code_cache[change.old] = self.code_objects(original, str(self.source / change.old))
                        if change.old not in self._expression_load_anchors:
                            self._expression_load_anchors[change.old] = load_anchors(self.code_cache[change.old])
                        return self._expression_load_anchors[change.old]
                    self.expression_plan = build_plan(
                        change.old, original, modified, tree, touched, insertions,
                        None, self.map.get('collection_lines', {}).get(change.old, ()), anchors)
                    if self.expression_plan is not None:
                        p = self.expression_plan
                        self.expression_last = dict(path=p.path, kind=p.expression_kind,
                                                    span=p.expression_span, anchors=sorted(p.anchor_lines),
                                                    collection_touched=p.collection_touched)
                except (OSError, ValueError, SyntaxError, TypeError, KeyError, RecursionError, OverflowError):
                    pass
            return super().select_change(change)
        finally:
            self.expression_plan = previous

    def line_impact(self, path, line, nodes, insertion=False):
        plan = self.expression_plan
        if plan is not None and path == plan.path and line in plan.touched and not insertion:
            # Consult the current collection mask: ImportStateMixin temporarily
            # clears it only after obtaining a successful state difference.
            if set(plan.anchor_lines) & set(self.map.get('collection_lines', {}).get(path, ())):
                return self.all_bits
            bits = 0
            for anchor in plan.anchor_lines:
                bits |= self.lines(path, anchor, anchor)
            return bits
        return super().line_impact(path, line, nodes, insertion=insertion)
