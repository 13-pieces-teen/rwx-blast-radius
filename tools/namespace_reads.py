"""Observe class namespace consumption without replacing namespace objects.

Only the built-in mappingproxy.get operation has key-specific semantics here.
Every other namespace access remains opaque, including iteration and aliases.
"""
import ast
import importlib.abc
import importlib.machinery
from pathlib import Path
import sys
import types


_TYPE_NAMESPACE = type.__dict__["__dict__"]
_TYPE_MRO = type.__dict__["__mro__"]


def ordinary_class_namespace(obj):
    """A custom metaclass may return a derived mappingproxy for __dict__."""
    if not isinstance(obj, type):
        return False
    slots = {}
    # Use built-in descriptors directly so this check cannot invoke metaclass
    # overrides while inspecting the lookup behavior it is meant to validate.
    for base in _TYPE_MRO.__get__(type(obj)):
        namespace = _TYPE_NAMESPACE.__get__(base)
        for name in ("__dict__", "__getattribute__"):
            if name not in slots and name in namespace:
                slots[name] = namespace[name]
    return (slots.get("__dict__") is _TYPE_NAMESPACE
            and slots.get("__getattribute__") is type.__getattribute__)


class NamespaceObserver(ast.NodeTransformer):
    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr == "__dict__":
            return ast.copy_location(ast.Call(func=ast.Name(id="__rwx_namespace_read__", ctx=ast.Load()),
                                             args=[node.value], keywords=[]), node)
        return node

    def visit_Call(self, node):
        # Visit the receiver before the generic traversal consumes __dict__.
        # Returning a callable preserves method lookup before argument evaluation.
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "get"
                and isinstance(func.value, ast.Attribute) and func.value.attr == "__dict__"):
            node.func = ast.copy_location(ast.Call(
                func=ast.Name(id="__rwx_namespace_get__", ctx=ast.Load()),
                args=[self.visit(func.value.value)], keywords=[]), func)
            node.args = [self.visit(arg) for arg in node.args]
            node.keywords = [self.visit(keyword) for keyword in node.keywords]
            return node
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id in ("vars", "getattr", "hasattr") and node.args:
            return ast.copy_location(ast.Call(func=ast.Name(id="__rwx_namespace_dynamic__", ctx=ast.Load()),
                                             args=[node.func, *node.args], keywords=node.keywords), node)
        return node


def helpers(record):
    def read(obj):
        value = obj.__dict__
        record(obj, None)
        return value

    def get_method(obj):
        namespace = obj.__dict__
        method = namespace.get
        if type(namespace) is not types.MappingProxyType or not ordinary_class_namespace(obj):
            record(obj, None)
            return method

        def get(*args, **kwargs):
            key = args[0] if args and type(args[0]) is str and not kwargs else None
            record(obj, key)
            return method(*args, **kwargs)

        return get

    def dynamic(function, *values, **kwargs):
        value = function(*values, **kwargs)
        if values and (function is vars or
                       ((function is getattr or function is hasattr)
                        and len(values) > 1 and values[1] == "__dict__")):
            record(values[0], None)
        return value

    return {"__rwx_namespace_read__": read, "__rwx_namespace_get__": get_method,
            "__rwx_namespace_dynamic__": dynamic}


def install(record):
    observer_helpers = helpers(record)

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            tree = ast.parse(Path(self.path).read_text(encoding="utf-8"), filename=self.path)
            return compile(ast.fix_missing_locations(NamespaceObserver().visit(tree)), self.path, "exec", dont_inherit=True)

        def exec_module(self, module):
            module.__dict__.update(observer_helpers)
            super().exec_module(module)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname not in ("sqlglot", "tests") and not fullname.startswith(("sqlglot.", "tests.")):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and spec.origin and spec.origin.endswith(".py"):
                spec.loader = Loader(fullname, spec.origin)
            return spec

    sys.meta_path.insert(0, Finder())
