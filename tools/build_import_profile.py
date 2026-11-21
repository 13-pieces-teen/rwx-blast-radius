"""Observe eager baseline imports and test collection, without executing tests.

Lazy imports under a test context can otherwise hide dependencies on derived
module/class state. This is a supplement to a validated full-suite coverage map.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import gzip
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys

import pytest
from attribute_reads import install
from build_maps import Collector


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, default=Path("/src"))
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    source = args.source.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    os.chdir(source)
    sys.path.insert(0, str(source))
    collector = Collector(source, args.output)
    collector.class_dict_reads = defaultdict(set)
    calls = defaultdict(set)
    errors = []
    imported = []
    audit_active = False

    def audit(event, args):
        if audit_active:
            collector.audit(event, args)

    def profile(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "<lambda>":
            path = Path(frame.f_code.co_filename)
            if path.is_relative_to(source):
                calls[path.relative_to(source).as_posix()].add(frame.f_code.co_firstlineno)

    class CollectionErrors:
        def pytest_collectreport(self, report):
            if report.failed:
                errors.append({"phase": "collection", "module": report.nodeid})

    collector.cov.start()
    collector.switch("@collection")
    sys.addaudithook(audit)
    install(collector)
    audit_active = True
    previous = sys.getprofile()
    sys.setprofile(profile)
    code = 1
    try:
        collector.instrument()
        for path in sorted((source / "sqlglot").rglob("*.py")):
            if path.name == "__main__.py":
                continue
            module = path.relative_to(source).with_suffix("").as_posix().replace("/", ".")
            module = module.removesuffix(".__init__")
            try:
                importlib.import_module(module)
                imported.append(module)
            except Exception as exc:
                errors.append({"phase": "import", "module": module,
                               "error": type(exc).__name__ + ": " + str(exc)})
        code = pytest.main(["tests", "--collect-only", "--assert=plain", "-q", "-p", "no:cacheprovider"],
                           plugins=[collector, CollectionErrors()])
    finally:
        sys.setprofile(previous)
        collector.save()
        audit_active = False
    data = collector.cov.get_data()
    lines = {}
    for filename in sorted(data.measured_files()):
        path = Path(filename)
        if path.is_relative_to(source):
            lines[path.relative_to(source).as_posix()] = sorted(
                n for n, contexts in data.contexts_by_lineno(filename).items() if "@collection" in contexts)
    source_hashes = {}
    for base in ("sqlglot", "tests"):
        for path in sorted((source / base).rglob("*.py")):
            text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
            source_hashes[path.relative_to(source).as_posix()] = hashlib.sha256(text.encode()).hexdigest()
    result = {
        "source_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
        "mode": "eager imports plus collect-only; no test outcomes",
        "complete": code == 0 and not errors, "failures": errors,
        "ids": sorted(collector.items), "source_hashes": source_hashes,
        "collection_lines": lines,
        "collection_attributes": sorted(k for k, c in collector.attributes.items() if "@collection" in c),
        "collection_reads": sorted(k for k, c in collector.reads.items() if "@collection" in c),
        "collection_symbols": sorted(k for k, c in collector.symbols.items() if "@collection" in c),
        "collection_class_dict": sorted(k for k, c in collector.class_dict_reads.items() if "@collection" in c),
        "lambda_bodies": {k: sorted(v) for k, v in sorted(calls.items())},
        "imported_modules": imported,
        "generator_hashes": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                             for name in ("build_import_profile.py", "build_maps.py", "attribute_reads.py")},
    }
    payload = gzip.compress(json.dumps(result, separators=(",", ":"), sort_keys=True).encode(), mtime=0)
    (args.output / "import-profile.json.gz").write_bytes(payload)
    print(json.dumps({"complete": result["complete"], "tests": len(result["ids"]), "imports": len(imported),
                      "collection_attributes": len(result["collection_attributes"]),
                      "collection_class_dict": len(result["collection_class_dict"]),
                      "lambda_calls": sum(map(len, calls.values())), "failures": errors}), flush=True)
    raise SystemExit(0 if result["complete"] else 1)


if __name__ == "__main__":
    main()
