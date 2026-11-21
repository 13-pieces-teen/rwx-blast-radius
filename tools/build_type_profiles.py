"""Observe operand types on the untouched tree; never evaluate alternate operators.

The output is runtime type metadata, not mutation outcomes or failure labels.
"""
from __future__ import annotations
import argparse
import ast
from collections import defaultdict
import concurrent.futures
import enum
import gzip
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys

import pytest
from build_maps import Collector, InlineExecutor


class NoCoverage:
    def switch_context(self, context):
        pass


class TypeProfiler:
    def __init__(self, collector):
        self.collector = collector
        self.types = []
        self.type_indices = {}
        self.type_identities = {}
        self.keep_types = []
        self.sites = defaultdict(set)
        self.member_ids = {}
        self.slots = {}
        for name, cls in (("object", object), ("str", str), ("int", int), ("float", float),
                          ("bytes", bytes), ("list", list), ("tuple", tuple), ("dict", dict),
                          ("set", set), ("frozenset", frozenset)):
            for op in ("__eq__", "__ne__"):
                method = type.__getattribute__(cls, op)
                self.slots.setdefault(id(method), name)

    def describe(self, value):
        cls = type(value)
        eq = type.__getattribute__(cls, "__eq__")
        ne = type.__getattribute__(cls, "__ne__")
        key = (id(cls), id(eq), id(ne))
        index = self.type_indices.get(key)
        if index is None:
            self.keep_types.append((cls, eq, ne))
            index = len(self.types)
            self.type_indices[key] = index
            identity = self.type_identities.setdefault(id(cls), len(self.type_identities))
            description = {
                "type_identity": identity,
                "module": type.__getattribute__(cls, "__module__"),
                "name": type.__getattribute__(cls, "__qualname__"),
                "eq": self.slots.get(id(eq), "custom"),
                "ne": self.slots.get(id(ne), "custom"),
                "none": cls is type(None),
                "enum_values": None,
            }
            if issubclass(cls, enum.Enum) and description["eq"] in ("str", "int"):
                members = type.__getattribute__(cls, "_member_map_")
                distinct = {id(member): member for member in members.values()}
                self.member_ids[index] = set(distinct)
                scalar = str.__str__ if description["eq"] == "str" else int.__int__
                description["enum_values"] = [scalar(member) for member in distinct.values()]
            self.types.append(description)
        registered_member = id(value) in self.member_ids.get(index, ())
        return index, registered_member

    def compare(self, site, operation, left, right):
        left_type, left_member = self.describe(left)
        right_type, right_member = self.describe(right)
        self.sites[site].add((left_type, right_type, left_member, right_member))
        if operation == "Is":
            return left is right
        if operation == "IsNot":
            return left is not right
        if operation == "Eq":
            return left == right
        return left != right

    def install(self):
        profiler = self
        source = self.collector.source

        class Transform(ast.NodeTransformer):
            def __init__(self, relative):
                self.relative = relative

            def visit_Compare(self, node):
                self.generic_visit(node)
                if len(node.ops) != 1:
                    return node
                op = node.ops[0]
                has_none = any(isinstance(n, ast.Constant) and n.value is None
                               for n in (node.left, node.comparators[0]))
                if not isinstance(op, (ast.Is, ast.IsNot)) and not (has_none and isinstance(op, (ast.Eq, ast.NotEq))):
                    return node
                site = f"{self.relative}:{node.lineno}:{node.col_offset}"
                return ast.copy_location(ast.Call(
                    func=ast.Name(id="__rwx_observe_comparison__", ctx=ast.Load()),
                    args=[ast.Constant(site), ast.Constant(type(op).__name__), node.left, node.comparators[0]],
                    keywords=[]), node)

        class Loader(importlib.machinery.SourceFileLoader):
            def get_code(self, fullname):
                path = Path(self.path)
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=self.path)
                tree = ast.fix_missing_locations(Transform(path.relative_to(source).as_posix()).visit(tree))
                return compile(tree, self.path, "exec")

            def exec_module(self, module):
                module.__dict__["__rwx_observe_comparison__"] = profiler.compare
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, default=Path("/src"))
    p.add_argument("--output", type=Path, default=Path("/profiles"))
    p.add_argument("--cold-caches", action="store_true")
    args, pytest_args = p.parse_known_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.chdir(args.source)
    sys.path.insert(0, str(args.source))
    collector = Collector(args.source, args.output)
    collector.cov = NoCoverage()
    collector.cold_caches = args.cold_caches
    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    profiler = TypeProfiler(collector)
    profiler.install()
    code = pytest.main(pytest_args or ["tests", "-q", "-p", "no:cacheprovider", "--timeout=400", "--tb=short", "-rN"],
                       plugins=[collector])
    failures = [r for r in collector.results if r["outcome"] == "failed"]
    result = {"source_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
              "cold_caches": args.cold_caches,
              "complete": code == 0 and not failures,
              "ids": sorted(collector.items), "failures": failures,
              "types": profiler.types, "comparisons": {k: sorted(v) for k, v in profiler.sites.items()},
              "provenance": "Untouched pinned tests; observed operand types and enum metadata only; no alternative operator evaluated"}
    (args.output / "type-profiles.json.gz").write_bytes(gzip.compress(json.dumps(result,separators=(",", ":")).encode(),mtime=0))
    print(json.dumps({"complete": result["complete"], "tests":len(result["ids"]), "types":len(profiler.types),
                      "sites":len(profiler.sites), "failures":failures}), flush=True)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
