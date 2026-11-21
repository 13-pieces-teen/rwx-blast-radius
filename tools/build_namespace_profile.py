"""Profile specific and opaque class namespace reads in unchanged baseline tests."""
from __future__ import annotations
import argparse
from collections import defaultdict
import concurrent.futures
import gzip
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys

from namespace_reads import install


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, default=Path("/src"))
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--cold-caches", action="store_true")
    p.add_argument("--eager-import-only", action="store_true")
    args, pytest_args = p.parse_known_args()
    generator_names = ("build_namespace_profile.py", "namespace_reads.py", "build_maps.py")
    generator_hashes = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                        for name in generator_names}
    source = args.source.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    os.chdir(source)
    sys.path.insert(0, str(source))
    import pytest
    from build_maps import Collector, InlineExecutor
    from build_lambda_profile import NoCoverage
    from export_maps import validate_observations
    collector = Collector(source, args.output)
    collector.cov = NoCoverage()
    collector.cold_caches = args.cold_caches
    observed = {category: defaultdict(set) for category in ("all", "opaque", "fields")}
    source_hashes = {}
    for base in ("sqlglot", "tests"):
        for path in sorted((source / base).rglob("*.py")):
            text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
            if "__rwx_namespace_" in text:
                raise ValueError("observer helper name collision")
            source_hashes[path.relative_to(source).as_posix()] = hashlib.sha256(text.encode()).hexdigest()

    def record(obj, key):
        if not isinstance(obj, type) or not obj.__module__.startswith("sqlglot"):
            return
        module = sys.modules.get(obj.__module__)
        filename = getattr(module, "__file__", None)
        path = Path(filename) if filename else None
        if path and path.is_relative_to(source):
            symbol = path.relative_to(source).as_posix() + ":" + obj.__name__
            observed["all"][symbol].add(collector.context)
            if key is None:
                observed["opaque"][symbol].add(collector.context)
            else:
                observed["fields"][symbol + ":" + key].add(collector.context)

    errors = []

    class CollectionErrors:
        def pytest_collectreport(self, report):
            if report.failed:
                errors.append({"phase": "collection", "id": report.nodeid})

    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    install(record)
    if args.eager_import_only:
        for path in sorted((source / "sqlglot").rglob("*.py")):
            if path.name == "__main__.py":
                continue
            module = path.relative_to(source).with_suffix("").as_posix().replace("/", ".").removesuffix(".__init__")
            try:
                importlib.import_module(module)
            except Exception as exc:
                errors.append({"phase": "import", "id": module, "error": type(exc).__name__ + ": " + str(exc)})
    run_args = pytest_args or ["tests", "-q", "-p", "no:cacheprovider", "--timeout=900", "--tb=short", "-rN"]
    code = pytest.main(["--assert=plain", *(["--collect-only"] if args.eager_import_only else []), *run_args],
                       plugins=[collector, CollectionErrors()])
    failures = [r for r in collector.results if r["outcome"] == "failed"] + errors
    if not args.eager_import_only:
        try:
            validate_observations({"items": collector.items, "results": collector.results})
        except ValueError as exc:
            failures.append({"phase": "validation", "error": str(exc)})
    if any(hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() != digest
           for name, digest in generator_hashes.items()):
        failures.append({"phase": "validation", "error": "observer source changed during profiling"})
    ids = sorted(collector.items)
    positions = {name: i for i, name in enumerate(ids)}
    class_bits = defaultdict(int)
    for name, i in positions.items():
        class_bits[name.rsplit("::", 1)[0]] |= 1 << i
    mode = "eager-import-collection" if args.eager_import_only else ("cold" if args.cold_caches else "warm")
    result = {"source_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa", "ids": ids,
              "complete": code == 0 and not failures and bool(ids), "failures": failures,
              "mode": mode, "source_hashes": source_hashes,
              "generator_hashes": generator_hashes,
              "provenance": "Baseline mappingproxy.get keys; all other namespace reads opaque; original namespace objects retained"}
    for category, sites in observed.items():
        readers = {}
        collection = []
        for key, contexts in sorted(sites.items()):
            bits = 0
            for context in contexts:
                if context in positions:
                    bits |= 1 << positions[context]
                elif context.startswith("@class:"):
                    bits |= class_bits.get(context[7:], 0)
                elif context == "@collection":
                    collection.append(key)
            readers[key] = format(bits, "x")
        result[category] = {"readers": readers, "collection": collection}
    (args.output / "namespace-readers.json.gz").write_bytes(gzip.compress(
        json.dumps(result, separators=(",", ":"), sort_keys=True).encode(), mtime=0))
    print(json.dumps({"complete": result["complete"], "mode": mode, "tests": len(ids),
                      "sites": {k: len(v) for k, v in observed.items()}, "failures": failures}), flush=True)
    raise SystemExit(0 if result["complete"] else 1)


if __name__ == "__main__":
    main()
