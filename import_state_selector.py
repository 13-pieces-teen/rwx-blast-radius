"""Attribute consumers of observed import-state changes, plus runtime coverage."""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
import subprocess

from selector import apply_change, same_ast
from tools.import_state_probe import ImportStateProbe


class ImportStateMixin:
    def __init__(self, *args, import_state_profile=None, import_state_mode='readers', **kwargs):
        super().__init__(*args, **kwargs)
        self.import_state_mode = import_state_mode
        self.state_stats = Counter()
        self.state_last = None
        self._state_probe = None
        self._state_readers = None
        maps = args[1] if len(args) > 1 else kwargs['maps']
        self._state_path = Path(import_state_profile) if import_state_profile else Path(maps).parent/'import-state.json.gz'

    def select(self, patch):
        self.state_last = None
        return super().select(patch)

    def _state_record(self, reason, **details):
        self.state_stats[reason] += 1
        self.state_last = {'reason': reason, **details}

    def _state_relevant(self, path, tree, nodes, touched, insertions):
        collection = set(self.map.get('collection_lines', {}).get(path, ()))
        for line in touched | insertions:
            functions = [n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and n.body[0].lineno <= line <= n.end_lineno]
            if not functions:
                return True
            function = min(functions, key=lambda n: n.end_lineno-n.lineno)
            statements = [n for n in nodes if isinstance(n, ast.stmt) and n is not function
                          and function.body[0].lineno <= n.lineno <= line <= n.end_lineno]
            if line in insertions:
                if any(function.body[0].lineno <= n <= function.end_lineno for n in collection):
                    return True
            elif statements:
                statement = min(statements, key=lambda n: (n.end_lineno-n.lineno, -n.lineno))
                target = statement
                if isinstance(statement, (ast.If, ast.While)) and line <= statement.test.end_lineno:
                    target = statement.test
                elif isinstance(statement, (ast.For, ast.AsyncFor)) and line <= statement.iter.end_lineno:
                    target = statement.iter
                if any(target.lineno <= n <= target.end_lineno for n in collection):
                    return True
        return False

    def _state_runtime_bits(self, path, nodes, touched, insertions):
        # Import effects now have observed field consumers. Retain ordinary
        # runtime statement/condition coverage without the import => all gate.
        collection = self.map.get('collection_lines', {})
        old_lines = collection.get(path)
        old_lambdas = self.collection_lambda_bodies.get(path)
        old_sites = self.collection_lambda_sites
        collection[path] = []
        self.collection_lambda_bodies[path] = set()
        self.collection_lambda_sites = {s for s in old_sites if not s.startswith(path+':')}
        bits = 0
        try:
            for line in touched | insertions:
                functions = [n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                             and n.lineno <= line <= n.end_lineno]
                if functions:
                    function = min(functions, key=lambda n: n.end_lineno-n.lineno)
                    if line < function.body[0].lineno:
                        bits |= self.lines(path, function.body[0].lineno, function.end_lineno)
                    else:
                        bits |= self.line_impact(path, line, nodes, insertion=line in insertions)
                # Module/class declaration execution is represented by its
                # resulting state, not by every test that imported the module.
        finally:
            if old_lines is None:
                collection.pop(path, None)
            else:
                collection[path] = old_lines
            if old_lambdas is None:
                self.collection_lambda_bodies.pop(path, None)
            else:
                self.collection_lambda_bodies[path] = old_lambdas
            self.collection_lambda_sites = old_sites
        return bits

    def select_change(self, change):
        baseline = super().select_change(change)
        if self.import_state_mode == 'off' or not self.allow_operator_proof:
            return baseline
        path = change.old
        if (path != change.new or not path.startswith('sqlglot/') or not path.endswith('.py')
                or path not in self.map.get('source_hashes', {}) or path in self.collection_reads):
            return baseline
        if (self.lambda_body_only.get(path, False) and not self.import_needed
                and baseline != self.all_bits):
            # V8 already has actual lambda-call consumers. A changed callable
            # in a dispatch table must not become every reader of that table.
            self._state_record('existing_lambda_call_readers', path=path)
            return baseline
        try:
            original, tree, nodes = self.load_source(path)
            modified, touched, insertions = apply_change(original, change)
            after_tree = ast.parse(modified, filename=path)
            if same_ast(tree, after_tree) or not self._state_relevant(path, tree, nodes, touched, insertions):
                return baseline
            if self._state_probe is None:
                probe = ImportStateProbe(self.source, self._state_path, self.map)
                from import_state_readers import ImportStateReaders
                readers = ImportStateReaders(self)
                self._state_probe, self._state_readers = probe, readers
            changed, before, after = self._state_probe.compare({path: modified})
            # The snapshot performed the same complete SQLGlot import check.
            self.overrides[path] = modified
            self.import_needed = False
            if changed and self.import_state_mode == 'equal':
                self._state_record('state_changed', path=path, fields=len(changed))
                return baseline
            impact, details = self._state_readers.readers(changed, before, after)
            runtime = self._state_runtime_bits(path, nodes, touched, insertions)
            protected = self.bits('reads', path) | self.reflection_impact(path, original, modified)
            protected |= self.structural_impact(path, tree, after_tree, original, modified)
            result = runtime | impact | protected
            self._state_record('refined', path=path, fields=len(changed),
                               before_count=(baseline & self.all_bits).bit_count(),
                               after_count=(result & self.all_bits).bit_count(),
                               runtime_count=(runtime & self.all_bits).bit_count(),
                               state_count=(impact & self.all_bits).bit_count(), **details)
            return result
        except (OSError, ValueError, KeyError, TypeError, SyntaxError, RuntimeError, RecursionError,
                subprocess.TimeoutExpired) as error:
            self._state_record('snapshot_failed', path=path, error=type(error).__name__)
            return baseline
