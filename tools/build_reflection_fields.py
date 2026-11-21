"""Record which code-object fields the original tests and inspect module read."""
from __future__ import annotations
import argparse
import ast
from collections import defaultdict
import concurrent.futures
import gzip
import importlib.machinery
import json
import os
from pathlib import Path
import sys
import types


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("/src"))
    parser.add_argument("--output", type=Path, default=Path("/reflections"))
    args, pytest_args = parser.parse_known_args()
    args.source = args.source.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    os.chdir(args.source)
    sys.path.insert(0, str(args.source))
    observed = defaultdict(lambda: defaultdict(set))
    collector = None

    def read_field(value, field):
        if isinstance(value, types.CodeType):
            path = Path(value.co_filename)
            if path.is_relative_to(args.source):
                key = (path.relative_to(args.source).as_posix(), value.co_firstlineno, value.co_qualname)
                observed[key][field].add(collector.context if collector else "@collection")
        return getattr(value, field)

    class Transform(ast.NodeTransformer):
        def visit_Attribute(self, node):
            self.generic_visit(node)
            if isinstance(node.ctx, ast.Load) and node.attr.startswith("co_"):
                return ast.copy_location(ast.Call(func=ast.Name(id="__rwx_code_field__", ctx=ast.Load()),
                                                  args=[node.value, ast.Constant(node.attr)], keywords=[]), node)
            return node

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            tree = ast.parse(Path(self.path).read_text(encoding="utf-8"), filename=self.path)
            return compile(ast.fix_missing_locations(Transform().visit(tree)), self.path, "exec")

        def exec_module(self, module):
            module.__dict__["__rwx_code_field__"] = read_field
            super().exec_module(module)

    class Finder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "inspect" and fullname != "tests" and not fullname.startswith("tests."):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and spec.origin and spec.origin.endswith(".py"):
                spec.loader = Loader(fullname, spec.origin)
            return spec

    if "inspect" in sys.modules:
        raise RuntimeError("inspect was imported before reflection instrumentation")
    sys.meta_path.insert(0, Finder())
    import pytest
    from build_maps import Collector, InlineExecutor
    from build_type_profiles import NoCoverage

    collector = Collector(args.source, args.output)
    collector.cov = NoCoverage()
    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    # Pytest's assertion rewriter would otherwise bypass our loader for tests.
    code = pytest.main(["--assert=plain", *(pytest_args or ["tests", "-q", "-p", "no:cacheprovider", "--timeout=300", "--tb=short", "-rN"])], plugins=[collector])
    failures = [r for r in collector.results if r["outcome"] == "failed"]
    ids = sorted(collector.items)
    class_bits = defaultdict(int)
    for i, node in enumerate(ids):
        class_bits[node.rsplit("::", 1)[0]] |= 1 << i
    positions = {node: i for i, node in enumerate(ids)}
    functions = []
    for (path, line, name), fields in sorted(observed.items()):
        mapped = {}
        collection = []
        for field, contexts in sorted(fields.items()):
            bits = 0
            for context in contexts:
                if context in positions:
                    bits |= 1 << positions[context]
                elif context.startswith("@class:"):
                    bits |= class_bits.get(context[7:], 0)
                elif context == "@collection":
                    collection.append(field)
            if bits:
                mapped[field] = format(bits, "x")
        if mapped or collection:
            functions.append({"path": path, "line": line, "name": name, "fields": mapped, "collection": collection})
    result = {"source_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa", "ids": ids,
              "complete": code == 0 and not failures, "failures": failures, "functions": functions,
              "provenance": "Original pinned tests; actual code-object field reads in tests and Python inspect; no patches"}
    (args.output / "reflection-fields.json.gz").write_bytes(gzip.compress(json.dumps(result, separators=(",", ":")).encode(), mtime=0))
    print(json.dumps({"complete": result["complete"], "functions": len(functions), "failures": failures}), flush=True)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
