"""Measure shared executor transport on the pristine tree, without labels.

Run every test in each statically discovered class fixture. An inline executor
keeps native pickle boundaries visible; it preserves the distinction between
Future-local task/serialization errors and permanently broken unpickle/init
paths. This profile adds dependencies, never certifies mutant outcomes.
"""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
import concurrent.futures
from concurrent.futures.process import BrokenProcessPool
import gzip
import hashlib
import inspect
import json
import os
from pathlib import Path
import pickle
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared_executor_dependencies import discover_shared_pools, discover_local_factories

PHASES = ["initializer", "task_unpickle", "result_unpickle"]


class TransportExecutor(concurrent.futures.Executor):
    """Baseline observation model, not a substitute for native truth runs."""
    observer = None

    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        self.initializer, self.initargs = initializer, initargs
        self.initialized = self.broken = self.closed = False
        self.record = self.observer.created() if self.observer else None

    def phase(self, name, fn):
        if self.observer and self.record is not None:
            return self.observer.phase(self.record, name, fn)
        return fn()

    def poison(self, future, exc):
        self.broken = True
        error = BrokenProcessPool("initializer or executor transport unpickling failed")
        error.__cause__ = exc
        future.set_exception(error)

    def submit(self, fn, /, *args, **kwargs):
        if self.broken:
            raise BrokenProcessPool("the process pool is not usable anymore")
        if self.closed:
            raise RuntimeError("cannot schedule new futures after shutdown")
        if self.observer and self.record is not None:
            self.observer.submitted(self.record)
        future = concurrent.futures.Future()
        future.set_running_or_notify_cancel()
        if not self.initialized:
            self.initialized = True
            if self.initializer is not None:
                try:
                    def initialize():
                        initializer, initargs = pickle.loads(pickle.dumps((self.initializer, self.initargs)))
                        initializer(*initargs)
                    self.phase("initializer", initialize)
                except BaseException as exc:
                    self.poison(future, exc)
                    return future
        # A feeder-thread pickle error is attached to this Future, not the pool.
        try:
            task_bytes = pickle.dumps((fn, args, kwargs))
        except BaseException as exc:
            future.set_exception(exc)
            return future
        try:
            task, task_args, task_kwargs = self.phase("task_unpickle", lambda: pickle.loads(task_bytes))
        except BaseException as exc:
            self.poison(future, exc)
            return future
        failed = False
        try:
            value = task(*task_args, **task_kwargs)
        except BaseException as exc:
            failed, value = True, exc
        try:
            result_bytes = pickle.dumps(value)
        except BaseException as exc:
            # Native _sendback_result sends the serialization exception itself.
            failed = True
            try:
                result_bytes = pickle.dumps(exc)
            except BaseException as transport_exc:
                self.poison(future, transport_exc)
                return future
        try:
            value = self.phase("result_unpickle", lambda: pickle.loads(result_bytes))
        except BaseException as exc:
            self.poison(future, exc)
            return future
        if failed:
            future.set_exception(value)
        else:
            future.set_result(value)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.closed = True


def source_snapshot(source):
    result = {}
    for base in ("sqlglot", "tests"):
        for path in sorted((source / base).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                result[path.relative_to(source).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class Observer:
    def __init__(self, source, pools, output, local_factories=()):
        self.source, self.pools, self.output = source, pools, output
        self.context = "@collection"
        self.current_phase = None
        self.functions = {}
        self.filename_paths = {}
        self.records = {}
        self.errors = []
        self.local_factories = list(local_factories)
        self.observed_local_factories = defaultdict(int)
        self.unknown_factories = []
        self.collection = []
        self.selected = []
        self.finished = []
        self.reports = []
        self.started = time.monotonic()
        self.events = (output / "events.jsonl").open("x", encoding="utf8")
        for path in sorted((source / "sqlglot").rglob("*.py")):
            relative = path.relative_to(source).as_posix()
            self.filename_paths[str(path)] = relative
            tree = ast.parse(path.read_text(encoding="utf8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    first = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
                    entry = {"path": relative, "name": getattr(node, "name", "<lambda>"),
                             "first": first, "last": node.end_lineno}
                    self.functions[(str(path), first)] = entry
                    self.functions.setdefault((str(path), node.lineno), entry)

    def event(self, **values):
        self.events.write(json.dumps({"elapsed": time.monotonic() - self.started, **values}) + "\n")
        self.events.flush()

    def created(self):
        owner = self.context.rsplit("::", 1)[0]
        frames = inspect.stack(context=0)
        try:
            for spec in self.pools:
                if owner != f"{spec['path']}::{spec['class']}":
                    continue
                expected = str(self.source / spec.get("factory_path", spec["path"]))
                if any(frame.filename == expected and frame.lineno == spec["factory_line"] for frame in frames):
                    if spec["key"] in self.records:
                        self.errors.append({"duplicate_factory": spec["key"]})
                    record = {**spec, "observed_submissions": defaultdict(int),
                              "poison_lines": defaultdict(set), "poison_functions": {},
                              "phase_calls": defaultdict(int)}
                    self.records[spec["key"]] = record
                    self.event(event="shared_pool_created", key=spec["key"], context=self.context)
                    return record
            for spec in self.local_factories:
                if any(frame.filename == str(self.source / spec["path"])
                       and frame.lineno == spec["factory_line"] for frame in frames):
                    key = f"{spec['path']}:{spec['factory_line']}"
                    self.observed_local_factories[key] += 1
                    self.event(event="local_pool_created", key=key, context=self.context)
                    return None
        finally:
            del frames
        self.unknown_factories.append(self.context)
        self.errors.append({"unmatched_executor_factory": self.context})
        return None

    def submitted(self, record):
        record["observed_submissions"][self.context] += 1

    def trace(self, frame, event, arg):
        filename = frame.f_code.co_filename
        path = self.filename_paths.get(filename)
        if path is None:
            return None
        record = self.current_phase
        if event == "line":
            record["poison_lines"][path].add(frame.f_lineno)
        elif event == "call":
            key = (filename, frame.f_code.co_firstlineno)
            if key in self.functions:
                record["poison_functions"][key] = self.functions[key]
        return self.trace

    def phase(self, record, name, fn):
        previous_trace, previous_phase = sys.gettrace(), self.current_phase
        self.current_phase = record
        record["phase_calls"][name] += 1
        sys.settrace(self.trace)
        try:
            return fn()
        finally:
            sys.settrace(previous_trace)
            self.current_phase = previous_phase

    def pytest_collection_modifyitems(self, items):
        self.collection = [item.nodeid for item in items]
        # Full pristine suite also inventories dynamic/previously unknown
        # factories outside the statically discovered shared classes.
        self.selected = [item.nodeid for item in items]
        self.event(event="collection", all=self.collection, selected=self.selected)

    def pytest_runtest_logstart(self, nodeid, location):
        self.context = nodeid

    def pytest_runtest_logfinish(self, nodeid, location):
        self.finished.append(nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.errors.append({"collection_error": report.nodeid, "detail": report.longreprtext})

    def pytest_runtest_logreport(self, report):
        record = {"event": "report", "id": report.nodeid, "phase": report.when,
                  "kind": type(report).__name__, "subtest": type(report).__name__.lower() == "subtestreport",
                  "outcome": report.outcome, "seconds": report.duration}
        if report.failed:
            record["longreprtext"] = report.longreprtext
        self.reports.append(record)
        self.event(**record)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--maps", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "events.jsonl").exists():
        raise ValueError("refuse to overwrite a prior collection")
    base = json.loads(gzip.decompress(args.maps.read_bytes()))
    texts = {path.relative_to(source).as_posix(): path.read_text(encoding="utf8")
             for directory in ("sqlglot", "tests") for path in sorted((source / directory).rglob("*.py"))}
    hashes = {path: hashlib.sha256(texts[path].encode()).hexdigest() for path in base["source_hashes"]}
    if hashes != base["source_hashes"]:
        raise ValueError("source differs from the pinned baseline map")
    before = source_snapshot(source)
    (output / "source-before.json").write_text(json.dumps(before, sort_keys=True), encoding="utf8")
    pools = discover_shared_pools(texts, base["ids"])
    if not pools:
        raise ValueError("no shared executor discovered")
    local_factories = discover_local_factories(texts)
    observer = Observer(source, pools, output, local_factories)
    os.chdir(source)
    sys.path.insert(0, str(source))
    import pytest
    TransportExecutor.observer = observer
    concurrent.futures.ProcessPoolExecutor = TransportExecutor
    code = pytest.main(["tests", "-q", "-p", "no:cacheprovider", "--timeout=300", "--tb=short", "-rN"],
                       plugins=[observer])
    after = source_snapshot(source)
    (output / "source-after.json").write_text(json.dumps(after, sort_keys=True), encoding="utf8")
    failures = list(observer.errors)
    failures.extend(r for r in observer.reports if r["outcome"] == "failed")
    expected = set(base["ids"])
    if set(observer.selected) != expected or observer.finished != observer.selected:
        failures.append({"incomplete_test_lifecycle": True})
    if sorted(observer.collection) != sorted(base["ids"]):
        failures.append({"canonical_collection_mismatch": True})
    if before != after:
        failures.append({"source_modified": True})
    lifecycle_complete = True
    for nodeid in observer.selected:
        parent = [r for r in observer.reports if r["id"] == nodeid and not r["subtest"]]
        phases = [r["phase"] for r in parent]
        setup_skipped = any(r["phase"] == "setup" and r["outcome"] == "skipped" for r in parent)
        if (phases.count("setup") != 1 or phases.count("teardown") != 1
                or phases.count("call") != (0 if setup_skipped else 1)):
            lifecycle_complete = False
            failures.append({"incomplete_parent_reports": nodeid, "phases": phases})
    data = []
    for pool in pools:
        record = observer.records.get(pool["key"])
        if record is None or not record["observed_submissions"]:
            failures.append({"missing_pool_observations": pool["key"]})
            continue
        record["consumers"] = sorted(set(pool["static_consumers"]) | set(record["observed_submissions"]))
        record["poison_lines"] = {p: sorted(lines) for p, lines in sorted(record["poison_lines"].items())}
        record["poison_functions"] = sorted(record["poison_functions"].values(), key=lambda f: (f["path"], f["first"]))
        data.append(record)
    result = {"format_version": 1, "source_commit": base["source_commit"], "ids": base["ids"],
              "source_hashes": hashes, "complete": code == 0 and not failures, "failures": failures,
              "phases": PHASES, "pools": data, "pytest_exit": int(code),
              "full_suite_factory_audit": True, "unknown_factories": observer.unknown_factories,
              "lifecycle_complete": lifecycle_complete,
              "local_factories": local_factories, "observed_local_factories": observer.observed_local_factories,
              "fixture_source_hashes": {p: hashlib.sha256(t.encode()).hexdigest()
                                        for p, t in texts.items() if p.startswith("tests/")},
              "selected_tests": observer.selected, "finished_tests": observer.finished,
              "baseline_only": True, "failure_labels_or_patches_read": False,
              "source_snapshot_sha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
              "generator_hashes": {str(path.name): hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in [Path(__file__), Path(__file__).resolve().parents[1] / "shared_executor_dependencies.py"]},
              "environment": {"python": sys.version, "pytest": pytest.__version__,
                              "affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None},
              "limitations": ["Unknown dynamic factories fail this full-suite profile; class fixtures and explicit local-with pools are recognized.",
                              "Baseline transport dependencies are additive; native truth remains a separate validation."]}
    observer.event(event="summary", complete=result["complete"], pytest_exit=int(code), failures=failures)
    observer.events.close()
    (output / "shared-executor-profile.json.gz").write_bytes(gzip.compress(json.dumps(result, separators=(",", ":")).encode(), mtime=0))
    (output / "summary.json").write_text(json.dumps({k: v for k, v in result.items() if k not in {"ids", "source_hashes", "pools"}}, indent=2), encoding="utf8")
    print(json.dumps({"complete": result["complete"], "pools": len(data), "selected": len(observer.selected),
                      "consumers": [len(r["consumers"]) for r in data], "failures": failures}), flush=True)
    raise SystemExit(0 if result["complete"] else 1)


if __name__ == "__main__":
    main()
