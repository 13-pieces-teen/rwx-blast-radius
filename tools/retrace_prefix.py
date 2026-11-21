"""Retrace one baseline test after executing its complete original test prefix."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from build_maps import Collector
from export_maps import validate_observations, validate_coverage


class GatedCoverage:
    """Keep real collection and target coverage without tracing prefix execution."""
    def __init__(self, actual):
        self.actual = actual
        self.active = False
        self.context = "@collection"

    def switch_context(self, context):
        self.context = context
        if self.active:
            self.actual.switch_context(context)

    def start(self):
        if not self.active:
            self.actual.start()
            self.active = True
            self.actual.switch_context(self.context)

    def stop(self):
        if self.active:
            self.actual.stop()
            self.active = False

    def save(self):
        self.actual.save()


def validate_prefix(base, metadata):
    """Verify the executed prefix and its successful complete pytest lifecycle."""
    collection = metadata["full_collection"]
    if collection != base["items"]:
        raise ValueError("prefix collection differs from original order")
    target = metadata["target"]
    if collection.count(target) != 1:
        raise ValueError("target must occur exactly once in original collection")
    expected = collection[:collection.index(target) + 1]
    if metadata["executed_prefix"] != expected:
        raise ValueError("not every preceding test executed in original order")
    if metadata.get("cold_caches") != base.get("cold_caches", False):
        raise ValueError("prefix cache mode differs from original trace")
    if metadata.get("source_commit") != base.get("source_commit"):
        raise ValueError("prefix source commit differs from original trace")
    if metadata.get("collection_errors") or metadata.get("exit_code") != 0:
        raise ValueError("prefix run or collection failed")
    validate_observations({"items": expected, "results": metadata["prefix_results"]})
    phases = {row["when"]: row["outcome"] for row in metadata["prefix_results"] if row["id"] == target}
    if phases != {"setup": "passed", "call": "passed", "teardown": "passed"}:
        raise ValueError("target must pass all three pytest phases")


class PrefixCollector(Collector):
    def __init__(self, source, output, base, target):
        super().__init__(source, output)
        self.base = base
        self.target = target
        self.full_collection = []
        self.executed_prefix = []
        self.collection_errors = []
        self.cov = GatedCoverage(self.cov)

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items):
        super().pytest_collection_modifyitems(items)
        self.full_collection = list(self.items)
        if self.full_collection != self.base["items"] or self.full_collection.count(self.target) != 1:
            raise pytest.UsageError("full collection/order must match original raw trace")

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.nodeid)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session):
        if session.testsfailed:
            return True
        for index, item in enumerate(session.items):
            # Preserve the original next item so the target's teardown does not
            # prematurely finalize a shared class/module fixture.
            nextitem = session.items[index + 1] if index + 1 < len(session.items) else None
            self.executed_prefix.append(item.nodeid)
            item.ihook.pytest_runtest_protocol(item=item, nextitem=nextitem)
            if item.nodeid == self.target or session.shouldfail or session.shouldstop:
                break
        return True

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        if item.nodeid == self.target:
            self.cov.start()
        else:
            self.cov.stop()
        protocol = super().pytest_runtest_protocol(item, nextitem)
        next(protocol)
        try:
            yield
        finally:
            try:
                next(protocol)
            except StopIteration:
                pass
            self.cov.stop()
            if item.nodeid == self.target:
                self.switch("@post-prefix")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("/src"))
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--cold-caches", action="store_true")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("prefix output must be new or empty")
    base_bytes = (args.base / "observations.json").read_bytes()
    base = json.loads(base_bytes)
    if args.cold_caches != base.get("cold_caches", False):
        raise ValueError("requested cache mode differs from original trace")
    if args.target not in {row["id"] for row in base["results"] if row["outcome"] == "failed"}:
        raise ValueError("target must be a recorded failure in the original trace")
    source = args.source.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    os.chdir(source)
    sys.path.insert(0, str(source))
    names = ("retrace_prefix.py", "build_maps.py", "attribute_reads.py")
    generators = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}
    source_hashes = {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for folder in ("sqlglot", "tests") for path in sorted((source/folder).rglob("*.py"))}
    collector = PrefixCollector(source, args.output, base, args.target)
    collector.cold_caches = args.cold_caches
    collector.generator_hashes = generators
    collector.collection_reads_clean = True
    from attribute_reads import install
    install(collector)
    collector.cov.start()
    collector.switch("@collection")
    sys.addaudithook(collector.audit)
    collector.instrument()
    exit_code = pytest.main(["tests", "--assert=plain", "-q", "-p", "no:cacheprovider",
                             f"--timeout={args.timeout}", "--tb=short", "--maxfail=1", "-rN"],
                            plugins=[collector])
    metadata = {"mode": "full-prefix-target-coverage", "source_commit": base["source_commit"],
                "target": args.target, "full_collection": collector.full_collection,
                "executed_prefix": collector.executed_prefix, "prefix_results": collector.results,
                "cold_caches": args.cold_caches, "collection_errors": collector.collection_errors,
                "exit_code": int(exit_code), "base_observations_sha256": hashlib.sha256(base_bytes).hexdigest(),
                "generator_hashes": generators, "source_hashes": source_hashes,
                "complete": False}
    collector.items = [args.target]
    collector.results = [row for row in collector.results if row["id"] == args.target]
    # Successful prefix test contexts are already represented in the original
    # trace. Never relabel them as target readers or merge their extra observers.
    permitted = {args.target, "@collection", "@class:" + args.target.rsplit("::", 1)[0]}
    for category in ("reads", "symbols", "attributes", "modules"):
        values = getattr(collector, category)
        for key in list(values):
            values[key].intersection_update(permitted)
            if not values[key]:
                del values[key]
    collector.save()
    try:
        validate_prefix(base, metadata)
        validate_observations({"items": collector.items, "results": collector.results})
        validate_coverage(args.output, {"items": collector.items, "results": collector.results})
        if any(hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() != digest
               for name, digest in generators.items()):
            raise ValueError("collector sources changed during prefix run")
        metadata["complete"] = True
    finally:
        (args.output / "prefix-provenance.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps({"complete": metadata["complete"], "prefix_tests": len(metadata["executed_prefix"]),
                      "target": args.target, "target_results": collector.results}), flush=True)


if __name__ == "__main__":
    main()
