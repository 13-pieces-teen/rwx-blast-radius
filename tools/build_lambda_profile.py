"""Observe actual baseline lambda body executions, distinct from definitions."""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
import concurrent.futures
import gzip
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys

import pytest
from build_maps import Collector, InlineExecutor
from export_maps import validate_observations

OBSERVER = "__rwx_observe_lambda_body__"


class NoCoverage:
    def switch_context(self, context):
        pass


class LambdaObserver(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Lambda(self, node):
        self.generic_visit(node)
        site = f"{self.path}:{node.lineno}:{node.col_offset}"
        body = node.body
        # The tuple evaluates left-to-right. The original body is evaluated once
        # and returned unchanged; signature, locals, closure cells and defaults
        # are untouched. Reflection-dependent baseline tests must still pass.
        observe = ast.Call(func=ast.Name(id=OBSERVER, ctx=ast.Load()),
                           args=[ast.Constant(site)], keywords=[])
        node.body = ast.copy_location(ast.Subscript(
            value=ast.Tuple(elts=[observe, body], ctx=ast.Load()),
            slice=ast.Constant(1), ctx=ast.Load()), body)
        return node


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, default=Path("/src"))
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--cold-caches", action="store_true")
    p.add_argument("--eager-import-only", action="store_true")
    args, pytest_args = p.parse_known_args()
    source = args.source.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    os.chdir(source)
    sys.path.insert(0, str(source))
    collector = Collector(source, args.output)
    collector.cov = NoCoverage()
    collector.cold_caches = args.cold_caches
    calls = defaultdict(set)
    source_hashes = {}
    for path in sorted((source / "sqlglot").rglob("*.py")):
        relative = path.relative_to(source).as_posix()
        text = path.read_text(encoding="utf-8")
        source_hashes[relative] = hashlib.sha256(text.encode()).hexdigest()
        tree = ast.parse(text)
        if any(isinstance(n, ast.Name) and n.id == OBSERVER for n in ast.walk(tree)):
            raise ValueError("observer name collides with baseline code")
        for node in ast.walk(tree):
            if isinstance(node, ast.Lambda):
                calls[f"{relative}:{node.lineno}:{node.col_offset}"]
    errors = []

    def record(site):
        calls[site].add(collector.context)

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            path = Path(self.path)
            text = path.read_text(encoding="utf-8")
            relative = path.relative_to(source).as_posix()
            tree = ast.parse(text, filename=self.path)
            tree = ast.fix_missing_locations(LambdaObserver(relative).visit(tree))
            return compile(tree, self.path, "exec", dont_inherit=True)

        def exec_module(self, module):
            module.__dict__[OBSERVER] = record
            super().exec_module(module)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is not None and spec.origin and spec.origin.endswith(".py"):
                spec.loader = Loader(fullname, spec.origin)
            return spec

    class CollectionErrors:
        def pytest_collectreport(self, report):
            if report.failed:
                errors.append({"phase": "collection", "id": report.nodeid})

    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    sys.meta_path.insert(0, Finder())
    if args.eager_import_only:
        for path in sorted((source / "sqlglot").rglob("*.py")):
            if path.name == "__main__.py":
                continue
            module = path.relative_to(source).with_suffix("").as_posix().replace("/", ".").removesuffix(".__init__")
            try:
                importlib.import_module(module)
            except Exception as exc:
                errors.append({"phase": "import", "id": module,
                               "error": type(exc).__name__ + ": " + str(exc)})
    run_args = pytest_args or ["tests", "-q", "-p", "no:cacheprovider", "--timeout=900", "--tb=short", "-rN"]
    if args.eager_import_only:
        run_args = ["--collect-only", *run_args]
    code = pytest.main(run_args, plugins=[collector, CollectionErrors()])
    failures = [r for r in collector.results if r["outcome"] == "failed"] + errors
    if not args.eager_import_only:
        try:
            validate_observations({"items": collector.items, "results": collector.results})
        except ValueError as exc:
            failures.append({"phase": "validation", "error": str(exc)})
    ids = sorted(collector.items)
    positions = {name: i for i, name in enumerate(ids)}
    class_bits = defaultdict(int)
    for name, i in positions.items():
        class_bits[name.rsplit("::", 1)[0]] |= 1 << i
    readers = {}
    collection = []
    for site, contexts in sorted(calls.items()):
        value = 0
        for context in contexts:
            if context in positions:
                value |= 1 << positions[context]
            elif context.startswith("@class:"):
                value |= class_bits.get(context[7:], 0)
            elif context == "@collection":
                collection.append(site)
        readers[site] = format(value, "x")
    mode = "eager-import-collection" if args.eager_import_only else ("cold" if args.cold_caches else "warm")
    result = {
        "source_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
        "ids": ids, "complete": code == 0 and not failures and bool(ids), "failures": failures,
        "readers": readers, "collection": collection, "mode": mode,
        "source_hashes": source_hashes,
        "generator_hashes": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                             for name in ("build_lambda_profile.py", "build_maps.py")},
        "test_counts": {outcome: len({r['id'] for r in collector.results if r['when'] == 'call' and r['outcome'] == outcome})
                        for outcome in ('passed', 'failed', 'skipped')},
        "provenance": "Untouched pinned source; actual lambda body calls, including class setup and serialized process-pool work",
    }
    (args.output / "lambda-readers.json.gz").write_bytes(gzip.compress(json.dumps(result, separators=(",", ":"), sort_keys=True).encode(), mtime=0))
    print(json.dumps({"complete": result["complete"], "mode": mode, "tests": len(ids),
                      "sites": len(readers), "called_in_tests": sum(bool(int(v, 16)) for v in readers.values()),
                      "collection": len(collection), "failures": failures}), flush=True)
    raise SystemExit(0 if result["complete"] else 1)


if __name__ == "__main__":
    main()
