"""Observe original class namespace reads, including vars/getattr access."""
from __future__ import annotations
import argparse
import ast
from collections import defaultdict
import concurrent.futures
import gzip
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("/src"))
    parser.add_argument("--output", type=Path, required=True)
    args, pytest_args = parser.parse_known_args()
    args.source = args.source.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    os.chdir(args.source)
    sys.path.insert(0, str(args.source))
    observed = defaultdict(set)
    collector = None

    def record(obj):
        if isinstance(obj, type) and obj.__module__.startswith("sqlglot"):
            module = sys.modules.get(obj.__module__)
            path = Path(module.__file__) if module and getattr(module, "__file__", None) else None
            if path and path.is_relative_to(args.source):
                key = path.relative_to(args.source).as_posix() + ":" + obj.__name__
                observed[key].add(collector.context if collector else "@collection")

    def read(obj):
        value = obj.__dict__
        record(obj)
        return value

    def dynamic(function, *values, **kwargs):
        value = function(*values, **kwargs)
        if values and (function is vars or (function is getattr and len(values) > 1 and values[1] == "__dict__")):
            record(values[0])
        return value

    class Transform(ast.NodeTransformer):
        def visit_Attribute(self, node):
            self.generic_visit(node)
            if isinstance(node.ctx, ast.Load) and node.attr == "__dict__":
                return ast.copy_location(ast.Call(func=ast.Name(id="__rwx_class_dict__", ctx=ast.Load()),
                                                  args=[node.value], keywords=[]), node)
            return node

        def visit_Call(self, node):
            self.generic_visit(node)
            if isinstance(node.func, ast.Name) and node.func.id in ("vars", "getattr") and node.args:
                return ast.copy_location(ast.Call(func=ast.Name(id="__rwx_dict_dynamic__", ctx=ast.Load()),
                                                  args=[node.func, *node.args], keywords=node.keywords), node)
            return node

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            tree = ast.parse(Path(self.path).read_text(encoding="utf-8"), filename=self.path)
            return compile(ast.fix_missing_locations(Transform().visit(tree)), self.path, "exec")

        def exec_module(self, module):
            module.__dict__["__rwx_class_dict__"] = read
            module.__dict__["__rwx_dict_dynamic__"] = dynamic
            super().exec_module(module)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname not in ("sqlglot", "tests") and not fullname.startswith(("sqlglot.", "tests.")):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and spec.origin and spec.origin.endswith(".py"):
                spec.loader = Loader(fullname, spec.origin)
            return spec

    import pytest
    from build_maps import Collector, InlineExecutor
    from build_type_profiles import NoCoverage
    sys.meta_path.insert(0, Finder())
    collector = Collector(args.source, args.output)
    collector.cov = NoCoverage()
    collector.cold_caches = True
    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    code = pytest.main(["--assert=plain", *(pytest_args or ["tests", "-q", "-p", "no:cacheprovider", "--timeout=900"])],
                       plugins=[collector])
    ids = sorted(collector.items)
    positions = {name: i for i, name in enumerate(ids)}
    class_bits = defaultdict(int)
    for i, name in enumerate(ids):
        class_bits[name.rsplit("::", 1)[0]] |= 1 << i
    readers = {}
    collection = []
    for key, contexts in sorted(observed.items()):
        bits = 0
        for context in contexts:
            if context in positions:
                bits |= 1 << positions[context]
            elif context.startswith("@class:"):
                bits |= class_bits.get(context[7:], 0)
            elif context == "@collection":
                collection.append(key)
        if bits:
            readers[key] = format(bits, "x")
    failures = [r for r in collector.results if r["outcome"] == "failed"]
    result = {"source_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa", "ids": ids,
              "complete": code == 0 and not failures, "failures": failures,
              "readers": readers, "collection": collection,
              "provenance": "Unchanged pinned source; actual class namespace reads in SQLGlot and tests; cold dispatch cache per test"}
    (args.output / "class-dict-readers.json.gz").write_bytes(gzip.compress(json.dumps(result, separators=(",", ":")).encode(), mtime=0))
    print(json.dumps({"complete": result["complete"], "readers": len(readers), "collection": len(collection), "failures": failures}))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
