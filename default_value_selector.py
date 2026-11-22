"""Default-value dependencies composed with observed post-import state.

No patch IDs or outcome labels enter these plans. Ordinary function-body line
dependencies are the base; exact bool binding observations may exclude callers
that always supplied the opposite value. Import-state readers retain derived
fields and declaration edges instead of treating the function's own changed
default tuple as every user of its class.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field

@dataclass
class DefaultValueEdit:
    function: ast.FunctionDef | ast.AsyncFunctionDef
    qualname: str
    parameter: str
    kind: str
    old_value: object
    new_value: object
    nested: bool
    simple_descriptor: bool


@dataclass
class DefaultValuePlan:
    path: str
    edit: DefaultValueEdit
    body_bits: int
    runtime_bits: int
    metadata_bits: int
    all_bits: int
    binding_reason: str
    metadata_collection: bool = False
    can_skip_snapshot: bool = False
    diagnostics: dict = field(default_factory=dict)

    @property
    def bits(self):
        return self.runtime_bits | self.metadata_bits

    def reader_overrides(self, before, after, readers=None):
        return default_reader_overrides(self, before, after, readers)

    state_reader_overrides = reader_overrides


def _immutable_literal(node):
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return False, None
    def supported(item):
        return type(item) in (type(None), bool, int, float, complex, str, bytes) or type(item) is tuple and all(supported(v) for v in item)
    return supported(value), value


def _functions(tree):
    result = []
    def walk(node, scope=(), inside_function=False):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = '.'.join((*scope, child.name))
                result.append((qualname, child, inside_function))
                walk(child, (*scope, child.name), True)
            elif isinstance(child, ast.ClassDef):
                walk(child, (*scope, child.name), inside_function)
            else:
                walk(child, scope, inside_function)
    walk(tree)
    return result


def match_default_value_edit(before, after):
    """Accept exactly one existing immutable literal default replacement."""
    old_functions, new_functions = _functions(before), _functions(after)
    if len(old_functions) != len(new_functions):
        return None
    candidates = []
    for (qualname, old, nested), (new_qualname, new, _) in zip(old_functions, new_functions):
        if qualname != new_qualname or type(old) is not type(new):
            return None
        for slot, kind in (('defaults', 'positional'), ('kw_defaults', 'keyword_only')):
            previous, current = getattr(old.args, slot), getattr(new.args, slot)
            if len(previous) != len(current):
                return None
            for index, (left, right) in enumerate(zip(previous, current)):
                equal = ast.dump(left) == ast.dump(right) if left is not None and right is not None else left is right
                if equal:
                    continue
                if left is None or right is None:
                    return None
                left_ok, old_value = _immutable_literal(left)
                right_ok, new_value = _immutable_literal(right)
                if not left_ok or not right_ok:
                    return None
                if kind == 'positional':
                    args = [*old.args.posonlyargs, *old.args.args]
                    parameter = args[len(args) - len(previous) + index].arg
                else:
                    parameter = old.args.kwonlyargs[index].arg
                candidates.append((qualname, old, nested, current, index, left, right, parameter, kind, old_value, new_value))
    if len(candidates) != 1:
        return None
    qualname, function, nested, current, index, left, right, parameter, kind, old_value, new_value = candidates[0]
    current[index] = left
    try:
        if ast.dump(before) != ast.dump(after):
            return None
    finally:
        current[index] = right
    simple_descriptor = all(isinstance(d, ast.Name) and d.id in {'staticmethod', 'classmethod'} for d in function.decorator_list)
    return DefaultValueEdit(function, qualname, parameter, kind, old_value, new_value, nested, simple_descriptor)


def _generic_metadata_bits(selector):
    """Keep observed default/signature inspection sites for unprofiled slots.

    This is a reusable baseline source/coverage scan, not a per-function answer
    table. Class fixture coverage is already expanded by the baseline mapper.
    Test-module collection reads retain every test in that module.
    """
    cached = getattr(selector, '_default_value_metadata_bits', None)
    if cached is not None:
        return cached
    bits = 0
    tokens = ('__defaults__', '__kwdefaults__', 'signature', 'getfullargspec', 'getcallargs', 'from_callable', 'attrgetter')
    for path in selector.map.get('source_hashes', {}):
        if not path.endswith('.py'):
            continue
        cached_source = getattr(selector, 'cache', {}).get(path)
        source = getattr(selector, 'source', None)
        text = cached_source[0] if cached_source else (source / path).read_text(encoding='utf8') if source else selector.load_source(path)[0]
        if not any(token in text for token in tokens):
            continue
        text, tree, nodes = selector.load_source(path)
        aliases = {'signature', 'getfullargspec', 'getcallargs'}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == 'inspect':
                aliases.update(a.asname or a.name for a in node.names if a.name in aliases)
        def metadata(node):
            if isinstance(node, ast.Attribute) and node.attr in {'__defaults__', '__kwdefaults__'}:
                return True
            if not isinstance(node, ast.Call):
                return False
            target = node.func
            if isinstance(target, ast.Attribute) and target.attr in {'signature', 'getfullargspec', 'getcallargs', 'from_callable'}:
                return True
            if isinstance(target, ast.Name) and target.id in aliases:
                return True
            for argument in [*node.args, *(kw.value for kw in node.keywords)]:
                try:
                    name = ast.literal_eval(argument)
                except (ValueError, TypeError, SyntaxError):
                    continue
                if type(name) is str and name in {'__defaults__', '__kwdefaults__'}:
                    return True
            return False
        for node in nodes:
            if not metadata(node):
                continue
            bits |= selector.lines(path, node.lineno, node.end_lineno)
            if path.startswith('tests/') and any(node.lineno <= line <= node.end_lineno
                    for line in selector.map.get('collection_lines', {}).get(path, ())):
                for index, identity in enumerate(selector.map['ids']):
                    if identity.split('::', 1)[0] == path:
                        bits |= 1 << index
    selector._default_value_metadata_bits = bits
    return bits


def plan_default_value(selector, path, before, after, touched=None, insertions=None):
    """Return a call/metadata plan; caller retains import, source and pool edges."""
    edit = match_default_value_edit(before, after)
    if edit is None:
        return None
    first, last = edit.function.body[0].lineno, edit.function.end_lineno
    body = selector.lines(path, first, last)
    runtime, metadata, reason = body, 0, 'body_coverage'
    metadata_collection = False
    profile = getattr(selector, 'default_dependencies', None)
    site = f'{path}:{edit.function.lineno}:{edit.qualname}'
    record = profile.get('functions', {}).get(site) if profile else None
    if record:
        metadata = int(record.get('metadata_readers', '0'), 16)
        metadata_collection = bool(record.get('metadata_collection'))
    if not record or record.get('identity_opaque'):
        metadata |= _generic_metadata_bits(selector)
        reason = 'body_plus_generic_metadata'
    can_skip_snapshot = False
    if record and not record.get('identity_opaque'):
        parameter = record.get('parameters', {}).get(edit.parameter)
        if (type(edit.old_value) is bool and parameter and parameter.get('default') is edit.old_value
                and parameter.get('kind') == edit.kind):
            seen = int(parameter.get('seen', '0'), 16)
            possible = int(parameter.get('possible', '0'), 16) | int(parameter.get('opaque', '0'), 16)
            runtime = (body & ~seen) | possible
            reason = 'observed_bool_bindings'
            can_skip_snapshot = (not edit.nested and not edit.function.decorator_list
                and not metadata_collection
                and not parameter.get('collection_possible', True)
                and not parameter.get('collection_opaque', True))
    plan = DefaultValuePlan(path, edit, body, runtime, metadata, selector.all_bits, reason,
                            metadata_collection, can_skip_snapshot)
    plan.diagnostics = {'path': path, 'function': edit.qualname, 'line': edit.function.lineno,
        'parameter': edit.parameter, 'kind': edit.kind, 'old_type': type(edit.old_value).__name__,
        'new_type': type(edit.new_value).__name__, 'binding_reason': reason,
        'body_count': body.bit_count(), 'runtime_count': runtime.bit_count(),
        'metadata_count': metadata.bit_count(), 'metadata_collection': metadata_collection,
        'can_skip_snapshot': can_skip_snapshot,
        'nested': edit.nested, 'decorated': bool(edit.function.decorator_list),
        'generator_or_async': isinstance(edit.function, ast.AsyncFunctionDef) or any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(edit.function))}
    return plan


def default_reader_overrides(plan, before, after, readers=None):
    """Override callable-root reads while retaining alias/declaration traversal.

    Do not remove changed rows: cached defaults in tests or derived imported
    data remain reachable through StateReaders' dependency edges.
    """
    edit = plan.edit
    if edit.nested or not edit.simple_descriptor:
        return {}
    owner, separator, name = edit.qualname.rpartition('.')
    key = plan.path + (':' + owner + ':' if separator else '::') + (name or edit.qualname)
    keys = {key}
    for snapshot in (before, after):
        for group in snapshot.get('aliases', []):
            if key in group:
                keys.update(group)
    callable_kinds = {'builtins.function', 'builtins.classmethod', 'builtins.staticmethod', 'builtins.method'}
    keys = {key for key in keys if any(snapshot.get('fields', {}).get(key, {}).get('value_kind') in callable_kinds
                                       for snapshot in (before, after))}
    bits = plan.bits
    if plan.metadata_collection:
        edges = getattr(readers, 'names', {}).get('test_declaration_edges', {}) if readers is not None else {}
        if not any(edges.get(key) for key in keys):
            # This specific observed collection dependency lacks a captured
            # cache edge. It cannot be discarded just because calls are known.
            bits |= plan.all_bits
    return {key: bits for key in keys}
