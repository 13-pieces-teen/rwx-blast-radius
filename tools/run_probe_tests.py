"""Run a test subset in a fresh Python process and report failures, including subtests."""
import json
import os
from pathlib import Path
import sys

import pytest


class Recorder:
    def __init__(self, config):
        self.include = set(config["include"])
        self.failures = set()
        self.passed = set()
        self.skipped = set()
        self.collection_errors = []

    def pytest_collection_modifyitems(self, items):
        items[:] = [item for item in items if item.nodeid in self.include]

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failures.add(report.nodeid)
        elif report.skipped:
            self.skipped.add(report.nodeid)
        elif report.when == "call":
            self.passed.add(report.nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.nodeid)


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    os.chdir(config["source"])
    sys.path.insert(0, config["source"])
    recorder = Recorder(config)
    code = pytest.main(["tests", "-q", "-p", "no:cacheprovider", "--timeout=120", "--tb=short", "-rN", "--maxfail=1"],
                       plugins=[recorder])
    Path(config["result"]).write_text(json.dumps({
        "exit_code": code, "failed": sorted(recorder.failures), "passed": len(recorder.passed),
        "skipped": sorted(recorder.skipped), "collection_errors": recorder.collection_errors,
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
