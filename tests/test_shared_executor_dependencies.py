import ast
import copy
import difflib
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from concurrent.futures.process import BrokenProcessPool
from shared_executor_dependencies import SharedExecutorDependencies, discover_shared_pools, discover_local_factories
from tools.build_shared_executor_profile import TransportExecutor, Observer


def identity(value):
    return value


def ordinary_error():
    raise ValueError("one callable failed")


def fail_unpickle():
    raise RuntimeError("decoder failed")


def bad_initializer():
    raise RuntimeError("initializer failed")


class BadLoad:
    def __reduce__(self):
        return fail_unpickle, ()


class BadDump:
    def __reduce__(self):
        raise TypeError("not serializable")


def return_bad_load():
    return BadLoad()


def return_bad_dump():
    return BadDump()


class SharedExecutorDiscoveryTests(unittest.TestCase):
    def test_cross_module_inheritance_and_aliases_are_resolved(self):
        sources = {"tests/base.py": '''import concurrent.futures as cf
class PoolBase:
    @classmethod
    def setUpClass(cls):
        cls.pool = cf.ProcessPoolExecutor()
    def helper(self):
        return self.pool.submit(str, 1)
''', "tests/test_child.py": '''from .base import PoolBase as Parent
class TestChild(Parent):
    def test_run(self):
        self.helper()
'''}
        ids = ["tests/test_child.py::TestChild::test_run"]
        found = discover_shared_pools(sources, ids)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["factory_path"], "tests/base.py")
        self.assertEqual(found[0]["static_consumers"], ids)

    def test_local_factory_requires_explicit_context_manager(self):
        source = '''from concurrent.futures import ProcessPoolExecutor
def unknown():
    return ProcessPoolExecutor()
def test_local():
    with ProcessPoolExecutor() as pool:
        pool.submit(str, 1)
'''
        found = discover_local_factories({"tests/test_s.py": source})
        self.assertEqual([x["factory_line"] for x in found], [5])

    def test_unknown_dynamic_factory_is_recorded_as_profile_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            observer = Observer(root, [], root)
            observer.context = "tests/test_s.py::TestDynamic::test_run"
            with patch("tools.build_shared_executor_profile.inspect.stack", return_value=[]):
                self.assertIsNone(observer.created())
            self.assertEqual(observer.unknown_factories, [observer.context])
            self.assertTrue(observer.errors)
            observer.events.close()

    def test_class_lifetime_transitive_helpers_and_inherited_fixture(self):
        text = '''from concurrent.futures import ProcessPoolExecutor as Pool
class Base:
    @classmethod
    def setUpClass(cls):
        cls.executor = Pool()
    def dispatch(self):
        return self.executor.submit(str, 2)
class TestFirst(Base):
    def helper(self):
        return self.dispatch()
    def test_consumer(self):
        self.helper()
    def test_unrelated(self):
        pass
class TestSecond(Base):
    def test_other(self):
        self.dispatch()
class TestLocal:
    def setUp(self):
        self.executor = Pool()
    def test_local(self):
        self.executor.submit(str, 1)
'''
        ids = ["tests/test_s.py::TestFirst::test_consumer", "tests/test_s.py::TestFirst::test_unrelated",
               "tests/test_s.py::TestSecond::test_other", "tests/test_s.py::TestLocal::test_local"]
        pools = discover_shared_pools({"tests/test_s.py": text}, ids)
        self.assertEqual(len(pools), 2)
        self.assertEqual(pools[0]["static_consumers"], ids[:1])
        self.assertEqual(pools[1]["static_consumers"], ids[2:3])
        self.assertEqual(pools[0]["class_test_ids"], ids[:2])
        self.assertEqual(pools[0]["factory_line"], pools[1]["factory_line"])

    def test_unrelated_named_executor_does_not_claim_process_semantics(self):
        text = '''from mine import ProcessPoolExecutor
class TestX:
    @classmethod
    def setUpClass(cls):
        cls.pool = ProcessPoolExecutor()
    def test_one(self):
        self.pool.submit(str, 1)
'''
        self.assertEqual(discover_shared_pools({"tests/test_s.py": text}, ["tests/test_s.py::TestX::test_one"]), [])


class ExecutorFailureSemanticsTests(unittest.TestCase):
    def test_ordinary_task_exception_does_not_break_later_consumers(self):
        with TransportExecutor() as pool:
            with self.assertRaisesRegex(ValueError, "one callable"):
                pool.submit(ordinary_error).result()
            self.assertEqual(pool.submit(identity, 9).result(), 9)
            self.assertFalse(pool.broken)

    def test_task_pickle_exception_is_future_local(self):
        with TransportExecutor() as pool:
            with self.assertRaises(TypeError):
                pool.submit(identity, BadDump()).result()
            self.assertEqual(pool.submit(identity, 9).result(), 9)

    def test_result_pickle_exception_is_future_local(self):
        with TransportExecutor() as pool:
            with self.assertRaises(TypeError):
                pool.submit(return_bad_dump).result()
            self.assertEqual(pool.submit(identity, 9).result(), 9)

    def test_task_unpickle_exception_breaks_later_consumers(self):
        with TransportExecutor() as pool:
            with self.assertRaises(BrokenProcessPool):
                pool.submit(identity, BadLoad()).result()
            with self.assertRaises(BrokenProcessPool):
                pool.submit(identity, 9)

    def test_result_unpickle_exception_breaks_later_consumers(self):
        with TransportExecutor() as pool:
            with self.assertRaises(BrokenProcessPool):
                pool.submit(return_bad_load).result()
            with self.assertRaises(BrokenProcessPool):
                pool.submit(identity, 9)

    def test_initializer_exception_breaks_later_consumers(self):
        with TransportExecutor(initializer=bad_initializer) as pool:
            with self.assertRaises(BrokenProcessPool):
                pool.submit(identity, 1).result()
            with self.assertRaises(BrokenProcessPool):
                pool.submit(identity, 2)

    def test_independent_pool_remains_healthy(self):
        with TransportExecutor() as first, TransportExecutor() as second:
            with self.assertRaises(BrokenProcessPool):
                first.submit(return_bad_load).result()
            self.assertEqual(second.submit(identity, 3).result(), 3)


class DependencyExpansionTests(unittest.TestCase):
    SOURCE = """SETTING = 1
def decoder(value):
    if value is None:
        return SETTING
    return value
def ordinary_task(value):
    return value + 1
"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "profile.json.gz"
        fixture = Path(self.directory.name) / "tests/test_s.py"
        fixture.parent.mkdir()
        fixture.write_text("# fixture identity\n", encoding="utf8")
        ids = ["tests/test_s.py::TestPool::test_a", "tests/test_s.py::TestPool::test_b", "tests/test_s.py::TestPool::test_unrelated"]
        source = self.SOURCE
        class Selector:
            map = {"ids": ids, "source_commit": "pin", "source_hashes": {"sqlglot/s.py": hashlib.sha256(source.encode()).hexdigest()}}
            numbers = {0: 10, 1: 11, 2: 12}
            tests = [{"id": name, "number": number} for name, number in zip(ids, [10, 11, 12])]

            def load_source(self, path):
                return source, ast.parse(source), []
        self.selector = Selector()
        self.selector.source = Path(self.directory.name)
        self.profile = {**self.selector.map, "format_version": 1, "complete": True, "failures": [],
                        "phases": ["initializer", "task_unpickle", "result_unpickle"],
                        "full_suite_factory_audit": True, "unknown_factories": [], "lifecycle_complete": True,
                        "fixture_source_hashes": {"tests/test_s.py": hashlib.sha256(fixture.read_text(encoding="utf8").encode()).hexdigest()},
                        "pools": [{"lifetime": "class", "consumers": ids[:2], "static_consumers": ids[:1],
                                   "observed_submissions": {ids[0]: 2, ids[1]: 1},
                                   "poison_functions": [{"path": "sqlglot/s.py", "first": 2, "last": 5}],
                                   "poison_lines": {"sqlglot/s.py": [3, 4]}}]}

    def guard(self, profile=None):
        self.path.write_bytes(gzip.compress(json.dumps(profile or self.profile).encode()))
        return SharedExecutorDependencies(self.selector, self.path)

    def patch(self, old, new):
        return "".join(difflib.unified_diff(self.SOURCE.splitlines(True), self.SOURCE.replace(old, new).splitlines(True),
                                            fromfile="a/sqlglot/s.py", tofile="b/sqlglot/s.py"))

    def test_decode_patch_adds_consumers_but_not_whole_test_class(self):
        chosen = self.guard().expand(self.patch("value is None", "value is not None"), self.selector.tests[:1])
        self.assertEqual([t["number"] for t in chosen], [10, 11])

    def test_ordinary_task_body_does_not_expand_shared_pool(self):
        self.assertEqual(self.guard().impact(self.patch("value + 1", "value + 2")), 0)

    def test_unobserved_branch_in_executed_decoder_remains_conservative(self):
        self.assertEqual(self.guard().impact(self.patch("return value\n", "return value + 4\n")), 3)

    def test_global_declaration_reaches_transport_consumers(self):
        self.assertEqual(self.guard().impact(self.patch("SETTING = 1", "SETTING = 2")), 3)

    def test_missing_profile_fails_closed(self):
        with self.assertRaises(OSError):
            SharedExecutorDependencies(self.selector, self.path)

    def test_stale_or_failed_or_incomplete_profile_fails_closed(self):
        for field, value in [("source_commit", "other"), ("source_hashes", {}), ("ids", []),
                             ("complete", False), ("failures", ["failed"]), ("phases", [])]:
            profile = copy.deepcopy(self.profile)
            profile[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.guard(profile)

    def test_missing_static_consumer_fails_closed(self):
        profile = copy.deepcopy(self.profile)
        profile["pools"][0]["static_consumers"] = self.selector.map["ids"]
        with self.assertRaises(ValueError):
            self.guard(profile)

    def test_fixture_source_identity_is_validated(self):
        (self.selector.source / "tests/test_s.py").write_text("# changed\n")
        with self.assertRaises(ValueError):
            self.guard()

    def test_dynamic_consumer_union_is_preserved(self):
        profile = copy.deepcopy(self.profile)
        profile["pools"][0]["consumers"] = self.selector.map["ids"]
        self.assertEqual(self.guard(profile).impact(self.patch("value is None", "value is not None")), 7)
