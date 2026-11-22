"""Deterministic, field-level SQLGlot state after an isolated import.

This module imports only the standard library. Call snapshot in a fresh child;
it intentionally does not try to unload a previously imported SQLGlot variant.
Function body edits remain the line selector's responsibility. Functions stored
in data containers and generated closures additionally contribute their code.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import ast
import datetime
import enum
import functools
import hashlib
import importlib
import importlib.abc
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
import types
import typing

SCHEMA_VERSION = 1
MODULE_NOISE = frozenset({"__builtins__", "__cached__", "__file__", "__loader__", "__spec__"})
CLASS_NOISE = frozenset({"__dict__", "__weakref__", "_abc_impl"})
PRIMITIVES = (str, int, bool, float, complex, bytes, type(None))


def _type_name(value):
    cls = type(value)
    return type.__getattribute__(cls, "__module__") + "." + type.__getattribute__(cls, "__qualname__")


def _class_name(cls):
    return type.__getattribute__(cls, "__module__") + ":" + type.__getattribute__(cls, "__qualname__")


def _encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()


class _Normalizer:
    """Memoized value summaries; identity relations are reported separately."""
    def __init__(self, unordered_type_tuples=()):
        self.memo = {}
        self.active = set()
        self.keepalive = []
        self.unsupported = {}
        self.unknown_by_key = {}
        self.unknown_stack = []
        self.primitive_cache = {}
        self.types = Counter()
        self.unordered_type_tuples = set(unordered_type_tuples)

    def _children(self, values):
        # Preserve aliases among immediate container members without walking
        # the whole interpreter graph or serializing process-specific ids.
        aliases = {}
        result = []
        for index, value in enumerate(values):
            if type(value) not in PRIMITIVES and not isinstance(value, (enum.Enum, type)):
                previous = aliases.get(id(value))
                if previous is not None:
                    result.append(["alias", previous])
                    continue
                aliases[id(value)] = index
            result.append(self.digest(value, True))
        return result

    def code(self, code):
        # co_filename, line tables and address-based repr are deliberately absent.
        return [code.co_code.hex(), code.co_names, code.co_varnames, code.co_cellvars,
                code.co_freevars, code.co_argcount, code.co_posonlyargcount,
                code.co_kwonlyargcount, code.co_flags, code.co_exceptiontable.hex(),
                [self.code(v) if type(v) is types.CodeType else self.digest(v, True)
                 for v in code.co_consts]]

    def digest(self, value, include_code=False):
        kind = type(value)
        if kind in PRIMITIVES:
            if kind in (float, complex, bytes):
                value = repr(value)
            primitive_key = (kind, value)
            if primitive_key not in self.primitive_cache:
                self.primitive_cache[primitive_key] = hashlib.sha256(_encode([kind.__name__, value])).hexdigest()
            return self.primitive_cache[primitive_key]
        if value is Ellipsis or value is NotImplemented:
            return hashlib.sha256(type(value).__name__.encode()).hexdigest()
        key = (id(value), bool(include_code))
        if key in self.memo:
            if self.unknown_stack:
                self.unknown_stack[-1].update(self.unknown_by_key[key])
            return self.memo[key]
        name = _type_name(value)
        if key in self.active:
            return hashlib.sha256(_encode(["cycle", name])).hexdigest()
        self.active.add(key)
        self.keepalive.append(value)
        self.types[name] += 1
        self.unknown_stack.append(set())
        try:
            payload = self.normalize(value, include_code)
            result = hashlib.sha256(_encode(payload)).hexdigest()
            self.memo[key] = result
            return result
        finally:
            unknown = self.unknown_stack.pop()
            self.unknown_by_key[key] = unknown
            if self.unknown_stack:
                self.unknown_stack[-1].update(unknown)
            self.active.remove(key)

    def normalize(self, value, include_code):
        kind = type(value)
        name = _type_name(value)
        if isinstance(value, enum.Enum):
            return ["enum", _class_name(kind), value.name, self.digest(value.value, True)]
        if isinstance(value, type):
            return ["class", _class_name(value)]
        if kind is types.ModuleType:
            return ["module", types.ModuleType.__getattribute__(value, "__dict__").get("__name__")]
        if isinstance(value, dict) or kind is types.MappingProxyType:
            # Class-keyed dispatch maps are often constructed from sets of
            # classes, whose hashes use addresses even with PYTHONHASHSEED=0.
            # Canonicalize that mapping order; preserve ordinary dict order.
            rows = list(value.items())
            if rows and all(isinstance(k, type) for k, _ in rows):
                rows.sort(key=lambda item: _class_name(item[0]))
            result = [name, [[self.digest(k, True), v] for (k, _), v in zip(rows, self._children(v for _, v in rows))]]
            if isinstance(value, defaultdict):
                result.append(self.digest(value.default_factory, True))
            return result
        if kind in (tuple, list):
            if kind is tuple and id(value) in self.unordered_type_tuples:
                return ["unordered-type-tuple", sorted(self.digest(v, True) for v in value)]
            return [name, self._children(value)]
        if kind in (set, frozenset):
            return [name, sorted(self.digest(v, True) for v in value)]
        if kind in (type({}.keys()), type({}.values()), type({}.items())):
            return [name, self._children(value)]
        if kind is types.FunctionType:
            if not (value.__module__ or "").startswith("sqlglot"):
                return ["external-function", value.__module__, value.__qualname__]
            generated = "<lambda>" in value.__qualname__ or "<locals>" in value.__qualname__
            return ["function", value.__module__, value.__qualname__,
                    self.code(value.__code__) if include_code or generated else None,
                    self.digest(value.__defaults__, True), self.digest(value.__kwdefaults__, True),
                    self.digest(value.__closure__, True), self.digest(value.__dict__, True),
                    self.digest(value.__annotations__, True)]
        if kind is types.CellType:
            try:
                return ["cell", self.digest(value.cell_contents, True)]
            except ValueError:
                return ["cell", "empty"]
        if kind is types.MethodType:
            return ["bound-method", self.digest(value.__func__, include_code), self.digest(value.__self__, False)]
        if kind in (classmethod, staticmethod):
            return [name, self.digest(value.__func__, include_code)]
        if kind is property:
            return [name, *(self.digest(f, include_code) for f in (value.fget, value.fset, value.fdel))]
        if isinstance(value, functools.partial):
            return [name, self.digest(value.func, True), self.digest(value.args, True), self.digest(value.keywords, True)]
        if isinstance(value, re.Pattern):
            return [name, value.pattern, value.flags]
        if isinstance(value, (datetime.date, datetime.time, datetime.timedelta)):
            return [name, str(value)]
        if kind is types.CodeType:
            return ["code", self.code(value)]
        if kind in (types.BuiltinFunctionType, types.BuiltinMethodType,
                    types.MethodDescriptorType, types.WrapperDescriptorType,
                    types.GetSetDescriptorType, types.MemberDescriptorType):
            owner = getattr(value, "__objclass__", None)
            return [name, getattr(value, "__module__", None), getattr(value, "__qualname__", getattr(value, "__name__", None)),
                    _class_name(owner) if isinstance(owner, type) else None]
        if kind is types.GenericAlias or name.startswith("typing."):
            # typing repr is symbolic, unlike application object repr.
            return [name, repr(value)]
        if name in {"_thread.RLock", "_thread.lock", "_abc._abc_data"}:
            return ["runtime-synchronization", name]
        if name.startswith(("_frozen_importlib.", "_frozen_importlib_external.")):
            return ["import-machinery", name, getattr(value, "name", None)]
        if name == "logging.Logger":
            return [name, value.name, value.level, value.disabled, value.propagate]
        if name == "collections._tuplegetter":
            return [name, value.__doc__]
        # SQLGlot expression/template instances have ordinary slots. Reading raw
        # storage here avoids calling their SQL rendering or custom properties.
        state = []
        try:
            dictionary = object.__getattribute__(value, "__dict__")
        except (AttributeError, TypeError):
            dictionary = None
        if dictionary is not None:
            state.append(["dict", self.digest(dictionary, True)])
        for cls in type.__getattribute__(kind, "__mro__"):
            raw = type.__getattribute__(cls, "__dict__")
            for slot, descriptor in raw.items():
                if type(descriptor) is types.MemberDescriptorType:
                    try:
                        member = descriptor.__get__(value, kind)
                    except AttributeError:
                        continue
                    state.append([slot, self.digest(member, True)])
        if state:
            return [name, state]
        # Explicit stable identities for standard singleton marker objects.
        if name.startswith(("dataclasses._", "__future__.")):
            return [name]
        self.unsupported[id(value)] = name
        self.unknown_stack[-1].add(name)
        return ["unsupported-identity", name]


def _import(source, overrides, imports):
    source = Path(source)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))

    class Loader(importlib.abc.SourceLoader):
        def __init__(self, path, text):
            self.path, self.text = path, text
        def get_filename(self, fullname):
            return str(source / self.path)
        def get_data(self, path):
            return self.text.encode()
        def get_code(self, fullname):
            return compile(self.text, str(source / self.path), "exec", dont_inherit=True)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            prefix = fullname.replace(".", "/")
            for candidate in (prefix + ".py", prefix + "/__init__.py"):
                if candidate in overrides:
                    return importlib.util.spec_from_file_location(fullname, source / candidate,
                        loader=Loader(candidate, overrides[candidate]),
                        submodule_search_locations=[str(source / prefix)] if candidate.endswith("/__init__.py") else None)
            return None

    finder = Finder()
    sys.meta_path.insert(0, finder)
    try:
        # Baseline import order comes first; extra modified modules follow it.
        for path in dict.fromkeys([*imports, *overrides]):
            name = path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
            importlib.import_module(name)
    finally:
        sys.meta_path.remove(finder)


def _unordered_type_tuple_policy(source, overrides, modules, paths, previous=None):
    """Recognize tuple(set(...)) used only as isinstance/issubclass type sets.

    Only modules containing actual tuples of ordinary classes are parsed. A
    supplied baseline policy avoids reparsing unchanged source in each child.
    """
    previous_keys = None if previous is None else set(previous.get("unordered_type_tuple_fields", []))
    accepted, identifiers = [], set()
    for module_name, module in modules.items():
        path = paths[module_name]
        candidates = {name: value for name, value in vars(module).items()
                      if type(value) is tuple and value and all(type(c) is type for c in value)}
        if not candidates:
            continue
        if previous_keys is not None and path not in overrides:
            selected = {name for name in candidates if path + "::" + name in previous_keys}
        else:
            text = overrides.get(path)
            if text is None:
                text = (Path(source) / path).read_text(encoding="utf8")
            if "tuple" not in text:
                continue
            tree = ast.parse(text, filename=path)
            nodes = list(ast.walk(tree))
            reserved = {"tuple", "set", "isinstance", "issubclass"}
            if any((isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id in reserved)
                   or (isinstance(n, ast.arg) and n.arg in reserved)
                   or (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in reserved)
                   or (isinstance(n, ast.alias) and (n.asname or n.name.split(".")[0]) in reserved) for n in nodes):
                continue
            parents = {id(child): parent for parent in nodes for child in ast.iter_child_nodes(parent)}
            selected = set()
            for node in tree.body:
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    name, value = node.targets[0].id, node.value
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    name, value = node.target.id, node.value
                else:
                    continue
                if name not in candidates or not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                        and value.func.id == "tuple" and len(value.args) == 1 and not value.keywords):
                    continue
                argument = value.args[0]
                if not (isinstance(argument, ast.Set) or isinstance(argument, ast.Call)
                        and isinstance(argument.func, ast.Name) and argument.func.id == "set"):
                    continue
                loads = [n for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == name]
                def membership_only(load):
                    parent = parents[id(load)]
                    return (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
                            and parent.func.id in {"isinstance", "issubclass"} and len(parent.args) == 2
                            and parent.args[1] is load and not parent.keywords)
                if loads and all(membership_only(load) for load in loads):
                    selected.add(name)
        for name in sorted(selected):
            accepted.append(path + "::" + name)
            identifiers.add(id(candidates[name]))
    return {"unordered_type_tuple_fields": sorted(accepted)}, identifiers


def snapshot(source, overrides, imports, normalization_policy=None):
    """Import one source variant and return digests plus reader-map metadata."""
    started = time.perf_counter()
    if any(n == "sqlglot" or n.startswith("sqlglot.") for n in sys.modules):
        raise RuntimeError("snapshot requires a fresh child without SQLGlot imports")
    _import(source, overrides, imports)
    imported = time.perf_counter()
    modules = {n: m for n, m in sorted(sys.modules.items()) if n == "sqlglot" or n.startswith("sqlglot.")}
    paths = {}
    for name, module in modules.items():
        raw = vars(module)
        filename = raw.get("__file__", "")
        try:
            paths[name] = Path(filename).relative_to(source).as_posix()
        except ValueError:
            paths[name] = name.replace(".", "/") + ".py"
    policy, unordered_type_tuples = _unordered_type_tuple_policy(source, overrides, modules, paths, normalization_policy)
    normalizer = _Normalizer(unordered_type_tuples)
    fields, classes, root_objects = {}, {}, defaultdict(list)
    discovered = {}

    def discover(value):
        if not isinstance(value, type):
            return
        module = type.__getattribute__(value, "__module__")
        if module not in paths or id(value) in discovered:
            return
        discovered[id(value)] = value
        for item in type.__getattribute__(value, "__dict__").values():
            if isinstance(item, type):
                discover(item)
        for base in type.__getattribute__(value, "__bases__"):
            discover(base)

    for module in modules.values():
        for value in vars(module).values():
            discover(value)

    def symbol(cls):
        module = type.__getattribute__(cls, "__module__")
        qualname = type.__getattribute__(cls, "__qualname__")
        return paths.get(module, "@" + module) + ":" + qualname

    def field(key, value, record):
        record["digest"] = normalizer.digest(value, False)
        record["value_kind"] = _type_name(value)
        fields[key] = record
        if type(value) not in PRIMITIVES and not isinstance(value, enum.Enum):
            root_objects[id(value)].append(key)
        unknown = normalizer.unknown_by_key.get((id(value), False), set())
        if unknown:
            record["unsupported_types"] = sorted(unknown)

    for module_name, module in modules.items():
        path = paths[module_name]
        for name, value in sorted(vars(module).items()):
            if name in MODULE_NOISE:
                continue
            if name == "__path__":
                # Package search paths carry the source checkout location, not
                # semantic imported state. Preserve relative entries and order.
                value = [str(Path(entry).relative_to(source)).replace("\\", "/")
                         if Path(entry).is_relative_to(source) else str(entry) for entry in value]
            field(path + "::" + name, value, {"kind": "module", "path": path, "name": name})
    for cls in sorted(discovered.values(), key=symbol):
        path = paths[type.__getattribute__(cls, "__module__")]
        qualname = type.__getattribute__(cls, "__qualname__")
        own = type.__getattribute__(cls, "__dict__")
        mro = type.__getattribute__(cls, "__mro__")
        cls_symbol = symbol(cls)
        classes[cls_symbol] = {"path": path, "qualname": qualname, "class_name": type.__getattribute__(cls, "__name__"),
            "bases": [symbol(c) for c in type.__getattribute__(cls, "__bases__")], "mro": [symbol(c) for c in mro],
            "own_fields": sorted(n for n in own if n not in CLASS_NOISE)}
        # Store defining fields once. Reader mapping uses the explicit MRO and
        # own_fields to expand inheritance without 200k repeated method records.
        declared = {name: value for name, value in own.items() if name not in CLASS_NOISE}
        declared["__bases__"] = type.__getattribute__(cls, "__bases__")
        declared["__mro__"] = mro
        for name, value in sorted(declared.items()):
            field(cls_symbol + ":" + name, value, {"kind": "class", "path": path, "qualname": qualname,
                "class_name": classes[cls_symbol]["class_name"], "name": name,
                "defined_on": cls_symbol, "inherited": False})
    aliases = [sorted(keys) for keys in root_objects.values() if len(keys) > 1]
    aliases.sort()
    # Binding an equal but distinct object changes alias behavior. Fold the
    # stable field-name group into each field digest so a digest-only consumer
    # also observes this relation; no process-specific id crosses the boundary.
    for group in aliases:
        alias_digest = hashlib.sha256(_encode(group)).hexdigest()
        for key in group:
            fields[key]["digest"] = hashlib.sha256((fields[key]["digest"] + alias_digest).encode()).hexdigest()
    unsupported_fields = {key: record["unsupported_types"] for key, record in fields.items() if record.get("unsupported_types")}
    return {"schema_version": SCHEMA_VERSION, "fields": fields, "classes": classes, "aliases": aliases,
        "unsupported_fields": unsupported_fields, "normalization_policy": policy,
        "stats": {"modules": len(modules), "classes": len(classes), "fields": len(fields),
            "value_objects": len(normalizer.memo), "unsupported_types": dict(Counter(normalizer.unsupported.values())),
            "import_seconds": imported - started, "normalize_seconds": time.perf_counter() - imported}}


def diff(before, after):
    """Return concrete changed fields, including derived and inherited values."""
    if before.get("schema_version") != SCHEMA_VERSION or after.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("import-state schema mismatch")
    old, new = before["fields"], after["fields"]
    changed = []
    for key in sorted(old.keys() | new.keys()):
        previous, current = old.get(key), new.get(key)
        if previous is None:
            change = "added"
        elif current is None:
            change = "removed"
        elif previous["digest"] != current["digest"] or previous.get("defined_on") != current.get("defined_on"):
            change = "value"
        else:
            continue
        changed.append({**(current or previous), "key": key, "change": change})
    old_aliases = {tuple(group) for group in before.get("aliases", [])}
    new_aliases = {tuple(group) for group in after.get("aliases", [])}
    alias_changed = sorted({key for group in old_aliases ^ new_aliases for key in group})
    recorded = {row["key"] for row in changed}
    for key in alias_changed:
        if key not in recorded and (key in old or key in new):
            changed.append({**(new.get(key) or old[key]), "key": key, "change": "value", "alias_changed": True})
    return {"changed": changed, "alias_changed_fields": alias_changed,
        "classes": after["classes"], "unsupported_changed": [r["key"] for r in changed if r.get("unsupported_types")]}
