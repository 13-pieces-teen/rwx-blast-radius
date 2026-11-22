"""Bool-default refinement guarded by baseline metadata and call dependencies.

The support check restores the one changed default before comparing the whole
AST. Existing selection/import side effects run first. A complete, identity-bound
baseline profile supplies metadata and bound-argument observations. Unresolved
state effects keep the original declaration dependencies.
"""
from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
import gzip
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from selector import apply_change, same_ast

def valid_profile(profile, baseline):
    """Reject incomplete optional artifacts, including missing zero-valued fields."""
    if not isinstance(profile,dict):return False
    required={'format_version','complete','failures','source_commit','source_hashes','state_source_hashes','ids','modes','metadata_scope','identity_audit_complete','identity_audit_modes','state_guard_complete','functions','persistent_writer_tests','persistent_opaque_tests'}
    if not required <= profile.keys():return False
    expected={name:value for name,value in baseline.get('source_hashes',{}).items() if name.startswith('sqlglot/')} or baseline.get('source_hashes',{})
    if not (profile['format_version']==1 and profile['complete'] is True and profile['failures']==[]
            and profile['source_commit']==baseline.get('source_commit') and profile['source_hashes']==expected
            and profile['state_source_hashes']==baseline.get('source_hashes',{}) and profile['ids']==baseline['ids']
            and {'warm','cold','eager-import-collection'} <= set(profile['modes'])
            and profile['identity_audit_complete'] is True and {'warm','cold','eager-import-collection'} <= set(profile['identity_audit_modes'])
            and profile['metadata_scope']=='source-tests-inspect-getattr' and profile['state_guard_complete'] is True):return False
    limit=(1<<len(baseline['ids']))-1
    def bits(value):
        return isinstance(value,str) and bool(value) and all(c in '0123456789abcdef' for c in value) and 0<=int(value,16)<=limit
    if not bits(profile['persistent_writer_tests']) or not bits(profile['persistent_opaque_tests']):return False
    if not isinstance(profile['functions'],dict):return False
    for site,r in profile['functions'].items():
        if not isinstance(site,str) or not isinstance(r,dict):return False
        if not {'parameters','metadata_readers','metadata_collection','metadata_fixture','identity_opaque','state_effect'}<=r.keys():return False
        if not bits(r['metadata_readers']) or any(type(r[n]) is not bool for n in ('metadata_collection','metadata_fixture','identity_opaque')):return False
        if r['state_effect'] not in ('closed','persistent','opaque'):return False
        if not isinstance(r['parameters'],dict):return False
        for p in r['parameters'].values():
            phase_names=('collection','fixture','collection_possible','collection_opaque','fixture_possible','fixture_opaque')
            if not isinstance(p,dict) or not {'default','kind','seen','possible','opaque',*phase_names}<=p.keys():return False
            if type(p['default']) is not bool or p['kind'] not in ('positional','keyword_only'):return False
            if any(type(p[n]) is not bool for n in phase_names) or any(not bits(p[n]) for n in ('seen','possible','opaque')):return False
            if any((p[phase+'_possible'] or p[phase+'_opaque']) and not p[phase] for phase in ('collection','fixture')):return False
            if (int(p['possible'],16)|int(p['opaque'],16)) & ~int(p['seen'],16):return False
    return True


@dataclass(frozen=True)
class DefaultEdit:
    function: ast.FunctionDef
    old_default: ast.Constant
    new_default: ast.Constant
    parameter: str
    kind: str


def bool_default_edit(before, after):
    """Return one ordinary function's bool-default edit, or a rejection reason.

    Comparison ignores AST location metadata, exactly like production same_ast.
    Restoring the changed literal must make the entire file AST identical.
    """
    differing = []

    def compare(left, right):
        if type(left) is not type(right):
            return False
        if isinstance(left, ast.Constant) and isinstance(right, ast.Constant):
            if type(left.value) is bool and type(right.value) is bool and left.value is not right.value:
                if left.kind != right.kind:
                    return False
                differing.append((left, right))
                return len(differing) <= 1
        if isinstance(left, ast.AST):
            return all(compare(getattr(left, key, None), getattr(right, key, None)) for key in left._fields)
        if isinstance(left, list):
            return len(left) == len(right) and all(compare(a, b) for a, b in zip(left, right))
        # same_ast historically permits True == 1 in scalar fields; this matcher
        # deliberately also requires exact scalar types, above.
        return left == right

    if not compare(before, after) or len(differing) != 1:
        return None, 'not_one_bool_literal_only'
    old_default, new_default = differing[0]
    parents = {id(child): node for node in ast.walk(before) for child in ast.iter_child_nodes(node)}
    args = parents.get(id(old_default))
    function = parents.get(id(args)) if args is not None else None
    if not isinstance(args, ast.arguments) or type(function) is not ast.FunctionDef:
        return None, 'not_ordinary_function_default'
    if old_default in args.defaults:
        index = next(i for i, value in enumerate(args.defaults) if value is old_default)
        arguments = [*args.posonlyargs, *args.args]
        parameter = arguments[len(arguments)-len(args.defaults)+index].arg
        kind = 'positional'
    elif old_default in args.kw_defaults:
        index = next(i for i, value in enumerate(args.kw_defaults) if value is old_default)
        parameter = args.kwonlyargs[index].arg
        kind = 'keyword_only'
    else:
        return None, 'not_existing_default_slot'
    if function.decorator_list:
        return None, 'decorated_function'
    ancestor = parents.get(id(function))
    while ancestor is not None:
        if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return None, 'nested_function_definition'
        ancestor = parents.get(id(ancestor))
    descendants = [node for statement in function.body for node in ast.walk(statement)]
    if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for node in descendants):
        return None, 'nested_definition_in_body'
    if any(isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await)) for node in descendants):
        return None, 'generator_or_coroutine'
    changed_value = new_default.value
    try:
        new_default.value = old_default.value
        restored_equal = same_ast(before, after)
    finally:
        new_default.value = changed_value
    if not restored_equal:
        return None, 'restored_ast_mismatch'
    return DefaultEdit(function, old_default, new_default, parameter, kind), 'supported_bool_default'


class DefaultsMixin:
    """Cooperative with Selector or DefaultsMixin, GranularitySelector MRO.

    precision_defaults=False is exactly the superclass algorithm. Diagnostic
    counters contain source-local facts only, never expected selections.
    """

    def __init__(self, *args, precision_defaults=False, precision_default_bindings=True,
                 default_dependencies=None, **kwargs):
        self.precision_defaults = precision_defaults
        self.precision_default_bindings = precision_default_bindings
        self.defaults_stats = Counter()
        self.defaults_last = None
        self._defaults_header_sites = {}
        super().__init__(*args, **kwargs)
        maps = args[1] if len(args) > 1 else kwargs.get('maps')
        self.default_dependencies = None
        try:
            path = Path(default_dependencies) if default_dependencies else Path(maps).parent/'default-dependencies.json.gz'
            profile = json.loads(gzip.decompress(path.read_bytes()))
            if valid_profile(profile,self.map):self.default_dependencies = profile
        except Exception:
            pass

    @staticmethod
    def _default_site(tree, function, path):
        parents={id(child):node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        names=[function.name];node=parents.get(id(function))
        while node is not None:
            if isinstance(node,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)):names.append(node.name)
            node=parents.get(id(node))
        return f'{path}:{function.lineno}:{".".join(reversed(names))}'

    def _default_dependency_bits(self, path, tree, edit, body, protected=0):
        profile=self.default_dependencies
        if profile is None:return None,'missing_complete_default_dependencies'
        site=self._default_site(tree,edit.function,path)
        record=profile.get('functions',{}).get(site)
        if not record or record.get('identity_opaque'):return None,'unknown_function_identity'
        parameter=record.get('parameters',{}).get(edit.parameter)
        if not parameter or type(parameter.get('default')) is not bool or parameter['default'] is not edit.old_default.value or parameter.get('kind')!=edit.kind:
            return None,'unknown_parameter_binding'
        if (record.get('metadata_collection') or record.get('metadata_fixture')
                or any(parameter[n] for n in ('collection_possible','collection_opaque','fixture_possible','fixture_opaque'))):
            return None,'collection_default_dependency'
        first,last=edit.function.body[0].lineno,edit.function.end_lineno
        # An inline function's definition and body share a physical line. Its
        # module-level definition event is not evidence that the body ran.
        collection_body = any(first<=line<=last and not (first==edit.function.lineno==line)
                              for line in self.map.get('collection_lines',{}).get(path,[]))
        if collection_body and not parameter['collection']:
            return None,'unobserved_collection_body'
        metadata=int(record.get('metadata_readers','0'),16)
        possible=int(parameter.get('possible','0'),16)|int(parameter.get('opaque','0'),16)
        seen=int(parameter.get('seen','0'),16)
        selected=((body & ~seen)|possible) if self.precision_default_bindings else body|possible
        selected |= metadata
        # If no baseline context can use this default or its metadata, there is
        # no affected call from which a persistent side effect can propagate.
        if selected and record['state_effect']!='closed':return None,'unclosed_source_state_effect'
        if selected and any(isinstance(n,(ast.Global,ast.Nonlocal)) for statement in edit.function.body for n in ast.walk(statement)):
            return None,'nonlocal_state_write'
        # The state scanner considers potential writes in the complete test AST,
        # not only assignments reached by the baseline. Unsafe escape paths are
        # never justified by an observed opposite argument value.
        unsafe=int(profile.get('persistent_writer_tests','0'),16)|int(profile.get('persistent_opaque_tests','0'),16)
        if (selected|protected) & unsafe:return None,'potential_persistent_test_state'
        return selected,'observed_default_dependencies'

    def select(self, patch):
        self.defaults_last = None
        return super().select(patch)

    def _defaults_record(self, reason, **details):
        self.defaults_stats[reason] += 1
        self.defaults_last = {'reason': reason, **details}

    def select_change(self, change):
        baseline = super().select_change(change)
        if not self.precision_defaults:
            return baseline
        path = change.old
        if not getattr(self, 'allow_operator_proof', False):
            self._defaults_record('multiple_file_changes')
            return baseline
        if path != change.new or not path.endswith('.py') or path not in self.map.get('source_hashes', {}):
            self._defaults_record('unsupported_or_unknown_source')
            return baseline
        if path in self.collection_reads:
            self._defaults_record('collection_source_read', path=path)
            return baseline
        try:
            original, before, _ = self.load_source(path)
            modified, touched, insertions = apply_change(original, change)
            if insertions:
                self._defaults_record('inserted_lines', path=path)
                return baseline
            if path not in self._defaults_header_sites:
                self._defaults_header_sites[path] = {
                    line for function in ast.walk(before)
                    if type(function) is ast.FunctionDef
                    for value in [*function.args.defaults, *function.args.kw_defaults]
                    if isinstance(value, ast.Constant) and type(value.value) is bool
                    for line in range(value.lineno, value.end_lineno+1)
                }
            if not touched & self._defaults_header_sites[path]:
                self._defaults_record('no_bool_default_touched', path=path)
                return baseline
            after = ast.parse(modified, filename=path)
            edit, reason = bool_default_edit(before, after)
            if edit is None:
                self._defaults_record(reason, path=path)
                return baseline
            function = edit.function
            details = {'path': path, 'function': function.name, 'line': function.lineno,
                       'parameter': edit.parameter, 'kind': edit.kind}
            self.defaults_stats['supported_syntax'] += 1
            first, last = function.body[0].lineno, function.end_lineno
            # Keep the original safeguards. Superclass initialized compiled
            # caches and its import request; recomputing these unions must not
            # erase source-read, reflection, lexical or statement-shape edges.
            protected = self.bits('reads', path)
            protected |= self.reflection_impact(path, original, modified)
            protected |= self.structural_impact(path, before, after, original, modified)
            body = self.lines(path, first, last)
            dependent, dependency_reason = self._default_dependency_bits(path, before, edit, body, protected)
            if dependent is None:
                self._defaults_record(dependency_reason, **details)
                return baseline
            refined = dependent | protected
            # Even a one-line function can make the base return early from its
            # declaration/body overlap guard. Retain actual import validation.
            self.overrides[path] = modified
            self.import_needed = True
            # A refinement never adds dependencies outside the base result.
            # Unexpected protected/body edges reject narrowing, rather than
            # silently discarding the protection through an intersection.
            if refined & ~baseline:
                self._defaults_record('outside_baseline_guard', **details)
                return baseline
            self._defaults_record('accepted', **details,
                                  before_count=(baseline & self.all_bits).bit_count(),
                                  after_count=(refined & self.all_bits).bit_count(),
                                  protected_count=(protected & self.all_bits).bit_count(),
                                  body_count=(body & self.all_bits).bit_count(),
                                  metadata_and_bound_count=(dependent & self.all_bits).bit_count())
            return refined
        except (OSError, ValueError, SyntaxError, TypeError, KeyError, IndexError, RecursionError, OverflowError):
            # A failed optional analysis cannot invalidate the base answer.
            self._defaults_record('analysis_error', path=path)
            return baseline
