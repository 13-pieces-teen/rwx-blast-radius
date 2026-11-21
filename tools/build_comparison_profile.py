"""Collect bounded raw operands from the untouched pinned source and tests."""
import argparse
import concurrent.futures
import gzip
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys

from comparison_values import HELPER, ValueObserver


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("/src"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cold-caches", action="store_true")
    parser.add_argument("--eager-import-only", action="store_true")
    args, pytest_args = parser.parse_known_args()
    names = ("build_comparison_profile.py", "comparison_values.py", "build_maps.py")
    generators = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                  for name in names}
    source = args.source.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    os.chdir(source)
    sys.path.insert(0, str(source))
    import pytest
    from build_maps import Collector, InlineExecutor
    from build_lambda_profile import NoCoverage
    from export_maps import validate_observations

    hashes = {}
    for base in ("sqlglot", "tests"):
        for path in sorted((source / base).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if HELPER in text:
                raise ValueError("observer name collision")
            hashes[path.relative_to(source).as_posix()] = hashlib.sha256(text.encode()).hexdigest()
    collector = Collector(source, args.output)
    collector.cov = NoCoverage()
    collector.cold_caches = args.cold_caches
    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    observer = ValueObserver()
    observer.install(source)
    errors = []

    class CollectionErrors:
        def pytest_collectreport(self, report):
            if report.failed:
                errors.append({"phase": "collection", "id": report.nodeid})

    if args.eager_import_only:
        for path in sorted((source / "sqlglot").rglob("*.py")):
            if path.name == "__main__.py":
                continue
            module = path.relative_to(source).with_suffix("").as_posix().replace("/", ".").removesuffix(".__init__")
            try:
                importlib.import_module(module)
            except Exception as exc:
                errors.append({"phase": "import", "id": module, "error": str(exc)})
    run_args = pytest_args or ["tests", "-q", "-p", "no:cacheprovider", "--timeout=900", "--tb=short", "-rN"]
    code = pytest.main([*(["--collect-only"] if args.eager_import_only else []), *run_args],
                       plugins=[collector, CollectionErrors()])
    failures = [r for r in collector.results if r["outcome"] == "failed"] + errors
    if not args.eager_import_only:
        try:
            validate_observations({"items": collector.items, "results": collector.results})
        except ValueError as exc:
            failures.append({"phase": "validation", "error": str(exc)})
    if any(hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() != digest
           for name, digest in generators.items()):
        failures.append({"phase": "validation", "error": "observer changed during profiling"})
    mode = "eager-import-collection" if args.eager_import_only else ("cold" if args.cold_caches else "warm")
    sites = {site: {**record, "observations": sorted(record["observations"])}
             for site, record in sorted(observer.sites.items())}
    result = {"source_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa", "ids": sorted(collector.items),
              "complete": code == 0 and not failures and bool(collector.items), "failures": failures,
              "mode": mode, "source_hashes": hashes, "generator_hashes": generators,
              "sites": sites, "provenance": "Raw builtin operands on unchanged baseline; no alternate operator or mutation evaluated"}
    (args.output / "comparison-values.json.gz").write_bytes(gzip.compress(
        json.dumps(result, separators=(",", ":"), sort_keys=True).encode(), mtime=0))
    print(json.dumps({"complete": result["complete"], "mode": mode, "tests": len(result["ids"]),
                      "sites": len(sites), "precise": sum(not r["opaque"] for r in sites.values()),
                      "failures": failures}), flush=True)
    raise SystemExit(0 if result["complete"] else 1)


if __name__ == "__main__":
    main()
