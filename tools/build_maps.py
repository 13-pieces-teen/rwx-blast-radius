"""Measure the unmodified pinned tree; never reads patches or failure labels."""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import functools
import hashlib
import json
import os
import pickle
from pathlib import Path
import sys
import unittest

import coverage
import pytest


class InlineExecutor:
    """Keep child work in its originating test context during map generation."""
    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        self.initializer = initializer
        self.initargs = initargs
        self.initialized = False

    def submit(self, fn, *args, **kwargs):
        future = concurrent.futures.Future()
        future.set_running_or_notify_cancel()
        try:
            if not self.initialized:
                self.initialized = True
                if self.initializer:
                    initializer, initargs = pickle.loads(pickle.dumps((self.initializer, self.initargs)))
                    initializer(*initargs)
            # Process pools serialize both the work item and the result. These
            # reductions exercise SQLGlot's serde paths and must remain traced.
            task, task_args, task_kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            result = task(*task_args, **task_kwargs)
            future.set_result(pickle.loads(pickle.dumps(result)))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.shutdown()


class Collector:
    def __init__(self, source: Path, output: Path):
        self.source = source.resolve()
        self.output = output
        self.context = "@collection"
        self.reads = collections.defaultdict(set)
        self.symbols = collections.defaultdict(set)
        self.attributes = collections.defaultdict(set)
        self.modules = collections.defaultdict(set)
        self.results = []
        self.items = []
        self.cold_caches = False
        self.generator_hashes = {}
        self.collection_reads_clean = False
        self.cov = coverage.Coverage(
            source=[str(source / "sqlglot"), str(source / "tests")],
            config_file=False, data_file=str(output / ".coverage"),
        )

    def switch(self, context):
        self.context = context
        self.cov.switch_context(context)

    def audit(self, event, args):
        if event != "open" or not isinstance(args[0], (str, bytes)):
            return
        try:
            path = os.fsdecode(args[0])
            if not os.path.isabs(path):
                path = os.path.join(os.getcwd(), path)
            relative = os.path.relpath(path, self.source).replace(os.sep, "/")
            if relative.endswith((".py", ".pyc")):
                frame = sys._getframe(1)
                while frame is not None:
                    if "importlib" in frame.f_code.co_filename:
                        return
                    frame = frame.f_back
            if not relative.startswith("../") and not relative.startswith(".git/"):
                self.reads[relative].add(self.context)
        except (OSError, ValueError):
            pass

    def instrument(self):
        concurrent.futures.ProcessPoolExecutor = InlineExecutor
        from sqlglot import exp
        from sqlglot.dialects.dialect import Dialect

        # Expr owns the constructor. Assigning it on Expression would shadow
        # cooperative TimeUnit constructors later in multiple-inheritance MROs.
        original_init = exp.Expr.__init__
        seen = set()

        @functools.wraps(original_init)
        def expression_init(instance, /, *args, **kwargs):
            cls = type(instance)
            key = (self.context, cls)
            if key not in seen:
                seen.add(key)
                for base in cls.__mro__:
                    if base.__module__.startswith("sqlglot"):
                        path = base.__module__.replace(".", "/") + ".py"
                        self.symbols[path + ":" + base.__name__].add(self.context)
                        self.modules[path].add(self.context)
            original_init(instance, *args, **kwargs)

        exp.Expr.__init__ = expression_init
        original_get = Dialect.get_or_raise.__func__
        seen_dialects = set()

        @classmethod
        def get_or_raise(cls, *args, **kwargs):
            result = original_get(cls, *args, **kwargs)
            dialect = type(result)
            key = (self.context, dialect)
            if key not in seen_dialects:
                seen_dialects.add(key)
                for component in (dialect, result.parser_class, result.generator_class,
                                  result.tokenizer_class):
                    for base in component.__mro__:
                        if base.__module__.startswith("sqlglot"):
                            path = base.__module__.replace(".", "/") + ".py"
                            self.modules[path].add(self.context)
                            self.symbols[path + ":" + base.__name__].add(self.context)
            return result

        Dialect.get_or_raise = get_or_raise

    def pytest_collection_modifyitems(self, items):
        self.items = [item.nodeid for item in items]

    @pytest.hookimpl(tryfirst=True)
    def pytest_pycollect_makeitem(self, collector, name, obj):
        # Pytest captures these bound methods while collecting the class.
        # collection_modifyitems runs after that capture and is too late.
        if not isinstance(obj, type) or not issubclass(obj, unittest.TestCase):
            return None
        for method_name in ("setUpClass", "tearDownClass", "doClassCleanups"):
            bound = getattr(obj, method_name, None)
            if bound is None:
                continue
            function = getattr(bound, "__func__", bound)
            if getattr(function, "__rwx_class_fixture__", False):
                continue
            receives_class = hasattr(bound, "__func__")

            @functools.wraps(function)
            def wrapped(cls, function=function, receives_class=receives_class):
                previous = self.context
                context = (previous if previous.startswith("@class:") or "::" not in previous else
                           "@class:" + previous.rsplit("::", 1)[0])
                self.switch(context)
                try:
                    return function(cls) if receives_class else function()
                finally:
                    self.switch(previous)

            wrapped.__rwx_class_fixture__ = True
            setattr(obj, method_name, classmethod(wrapped))
        return None

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.switch(item.nodeid)
        if self.cold_caches:
            # Dispatch is cached across tests. A warm-only trace attributes its
            # construction to the first consumer and misses subsequent users.
            # A separate cold run is unioned with the normal-order trace.
            from sqlglot.generator import _DISPATCH_CACHE
            _DISPATCH_CACHE.clear()
        yield
        self.switch("@collection")

    def pytest_runtest_logreport(self, report):
        if type(report).__name__ == "SubTestReport" and not report.failed:
            return
        self.results.append({"id": report.nodeid, "when": report.when,
                             "outcome": report.outcome, "seconds": report.duration})

    def save(self):
        self.cov.stop()
        self.cov.save()
        metadata = {
            "source_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
            "cold_caches": self.cold_caches,
            "generator_hashes": self.generator_hashes,
            "collection_reads_clean": self.collection_reads_clean,
            "items": self.items,
            "reads": {k: sorted(v) for k, v in self.reads.items()},
            "symbols": {k: sorted(v) for k, v in self.symbols.items()},
            "attributes": {k: sorted(v) for k, v in self.attributes.items()},
            "modules": {k: sorted(v) for k, v in self.modules.items()},
            "results": self.results,
        }
        (self.output / "observations.json").write_text(json.dumps(metadata), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("/src"))
    parser.add_argument("--output", type=Path, default=Path("/maps"))
    parser.add_argument("--attributes", action="store_true")
    parser.add_argument("--cold-caches", action="store_true")
    args, pytest_args = parser.parse_known_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.chdir(args.source)
    sys.path.insert(0, str(args.source))
    collector = Collector(args.source, args.output)
    collector.cold_caches = args.cold_caches
    collector.generator_hashes = {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in ("build_maps.py", "attribute_reads.py")
    }
    collector.cov.start()
    collector.switch("@collection")
    if args.attributes:
        from attribute_reads import install
        install(collector)
    # Scanning source to prepare the observer is not a program dependency.
    collector.collection_reads_clean = True
    sys.addaudithook(collector.audit)
    collector.instrument()
    try:
        # Assertion rewriting otherwise bypasses the loader observing direct
        # metadata reads in test modules.
        run_args = pytest_args or ["tests", "-q", "-p", "no:cacheprovider", "--timeout=900"]
        if args.attributes:
            run_args = ["--assert=plain", *run_args]
        code = pytest.main(run_args,
                           plugins=[collector])
    finally:
        collector.save()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
