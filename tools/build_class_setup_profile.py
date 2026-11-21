"""Observe class-wide setup/cleanup using one inexpensive baseline test per class.

The full collection supplies test identities. This is a supplemental fixture
trace, not a full-suite execution, and only @class contexts become dependencies.
"""
from __future__ import annotations
import argparse
import ast
from collections import defaultdict
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

import pytest
from attribute_reads import install
from build_maps import Collector


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("/src"))
    parser.add_argument("--tests", type=Path, default=Path(__file__).resolve().parents[1] / "sample/tests.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.source = args.source.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    os.chdir(args.source)
    sys.path.insert(0, str(args.source))
    durations = {t["id"]: t["seconds"] for t in json.loads(args.tests.read_text())["tests"]}
    sites = defaultdict(list)
    source_hashes = {}
    for base in ("sqlglot", "tests"):
        for path in sorted((args.source / base).rglob("*.py")):
            relative = path.relative_to(args.source).as_posix()
            source = path.read_text(encoding="utf-8")
            source_hashes[relative] = hashlib.sha256(source.encode()).hexdigest()
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Lambda):
                    sites[(str(path), node.lineno)].append(f"{relative}:{node.lineno}:{node.col_offset}")
    selected = []
    errors = []
    expected_classes = set()

    class SetupCollector(Collector):
        def pytest_collection_modifyitems(self, items):
            super().pytest_collection_modifyitems(items)
            grouped = defaultdict(list)
            for item in items:
                cls = getattr(item, "cls", None)
                if cls is None or not issubclass(cls, unittest.TestCase):
                    continue
                # unwrap our early collector hook to identify source-defined
                # fixture methods, rather than inherited unittest no-op methods.
                for method in ("setUpClass", "tearDownClass"):
                    function = getattr(getattr(cls, method), "__func__", getattr(cls, method))
                    while hasattr(function, "__wrapped__"):
                        function = function.__wrapped__
                    if Path(function.__code__.co_filename).is_relative_to(args.source):
                        grouped[item.nodeid.rsplit("::", 1)[0]].append(item)
                        break
            for name, group in sorted(grouped.items()):
                expected_classes.add(name)
                selected.append(min(group, key=lambda item: (durations.get(item.nodeid, float("inf")), item.nodeid)))
            items[:] = selected

        def pytest_collectreport(self, report):
            if report.failed:
                errors.append(report.nodeid)

    collector = SetupCollector(args.source, args.output)
    collector.class_dict_reads = defaultdict(set)
    lambda_reads = defaultdict(set)
    observed_classes = set()

    def profile(frame, event, arg):
        if event == "call" and collector.context.startswith("@class:"):
            observed_classes.add(collector.context[7:])
            if frame.f_code.co_name == "<lambda>":
                for site in sites.get((frame.f_code.co_filename, frame.f_code.co_firstlineno), []):
                    lambda_reads[site].add(collector.context)

    install(collector)
    collector.cov.start()
    collector.switch("@collection")
    sys.addaudithook(collector.audit)
    collector.instrument()
    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        code = pytest.main(["tests", "--assert=plain", "-q", "-p", "no:cacheprovider", "--timeout=300"],
                           plugins=[collector])
    finally:
        sys.setprofile(previous)
        collector.save()
    ids = sorted(collector.items)
    class_bits = defaultdict(int)
    for i, name in enumerate(ids):
        class_bits["@class:" + name.rsplit("::", 1)[0]] |= 1 << i

    def bits(contexts):
        value = 0
        for context in contexts:
            value |= class_bits.get(context, 0)
        return value

    data = collector.cov.get_data()
    lines = {}
    modules = defaultdict(int)
    for filename in sorted(data.measured_files()):
        path = Path(filename)
        if path.is_relative_to(args.source):
            relative = path.relative_to(args.source).as_posix()
            mapped = {str(n): format(bits(c), "x") for n, c in data.contexts_by_lineno(filename).items() if bits(c)}
            if mapped:
                lines[relative] = mapped
                for value in mapped.values():
                    modules[relative] |= int(value, 16)
    for path, contexts in collector.modules.items():
        modules[path] |= bits(contexts)
    failures = [r for r in collector.results if r["outcome"] == "failed"]
    if not expected_classes <= observed_classes:
        errors.append({"missing_class_contexts": sorted(expected_classes - observed_classes)})
    payload = {"source_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa", "ids": ids,
               "source_hashes": source_hashes, "complete": code == 0 and not errors and not failures,
               "failures": [*errors, *failures], "selected_tests": [item.nodeid for item in selected],
               "observed_classes": sorted(observed_classes), "lines": lines,
               "modules": {p: format(b, "x") for p, b in modules.items() if b},
               "provenance": "One cheapest baseline sentinel per source-defined class fixture; only actual setup, teardown and class-cleanup contexts; not a full-suite test run",
               "generator_hashes": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                    for name in ("build_class_setup_profile.py", "build_maps.py", "attribute_reads.py")}}
    for category in ("reads", "symbols", "attributes", "class_dict_reads"):
        payload[category] = {k: format(bits(c), "x") for k, c in getattr(collector, category).items() if bits(c)}
    payload["lambda_call_readers"] = {k: format(bits(c), "x") for k, c in lambda_reads.items() if bits(c)}
    (args.output / "class-setup-profile.json.gz").write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), mtime=0))
    print(json.dumps({"complete": payload["complete"], "tests": len(ids), "sentinels": payload["selected_tests"],
                      "classes": sorted(observed_classes), "lines": sum(map(len, lines.values())), "reads": len(payload["reads"]),
                      "failures": payload["failures"]}), flush=True)
    raise SystemExit(0 if payload["complete"] else 1)


if __name__ == "__main__":
    main()
