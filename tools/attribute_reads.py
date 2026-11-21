"""Transparent observation of class-configuration reads in the pinned source."""
import ast
from collections import defaultdict
import importlib.abc
import importlib.machinery
from pathlib import Path
import sys


def install(collector):
    fields = set()
    for path in (collector.source / "sqlglot").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                fields.update(t.id for t in targets if isinstance(t, ast.Name) and t.id.isupper())

    owner_cache = {}
    seen = set()

    def record(obj, name):
        if not isinstance(name, str) or name not in fields:
            return
        cls = obj if isinstance(obj, type) else type(obj)
        pair = (cls, name)
        if pair not in owner_cache:
            owner = next((c for c in cls.__mro__ if name in c.__dict__), None)
            key = None
            if owner is not None and owner.__module__.startswith("sqlglot"):
                key = owner.__module__.replace(".", "/") + ".py:" + owner.__name__ + ":" + name
            owner_cache[pair] = key
        key = owner_cache[pair]
        if key is not None and (collector.context, key) not in seen:
            seen.add((collector.context, key))
            collector.attributes[key].add(collector.context)

    def read(obj, name):
        value = getattr(obj, name)
        record(obj, name)
        return value

    def dynamic_read(function, obj, name, *default):
        value = function(obj, name, *default)
        record(obj, name)
        return value

    class Transform(ast.NodeTransformer):
        def visit_Attribute(self, node):
            self.generic_visit(node)
            if isinstance(node.ctx, ast.Load) and node.attr in fields:
                return ast.copy_location(ast.Call(func=ast.Name(id="__rwx_read_attribute__", ctx=ast.Load()),
                                                  args=[node.value, ast.Constant(value=node.attr)], keywords=[]), node)
            return node

        def visit_Call(self, node):
            self.generic_visit(node)
            if isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr") and len(node.args) >= 2:
                return ast.copy_location(ast.Call(func=ast.Name(id="__rwx_dynamic_attribute__", ctx=ast.Load()),
                                                  args=[node.func, *node.args], keywords=node.keywords), node)
            return node

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            text = Path(self.path).read_text(encoding="utf-8")
            tree = ast.fix_missing_locations(Transform().visit(ast.parse(text, filename=self.path)))
            return compile(tree, self.path, "exec")

        def exec_module(self, module):
            module.__dict__["__rwx_read_attribute__"] = read
            module.__dict__["__rwx_dynamic_attribute__"] = dynamic_read
            super().exec_module(module)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is not None and spec.origin and spec.origin.endswith(".py"):
                spec.loader = Loader(fullname, spec.origin)
            return spec

    sys.meta_path.insert(0, Finder())
