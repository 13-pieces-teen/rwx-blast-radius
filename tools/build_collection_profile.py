"""Observe actual lambda calls while collecting the unchanged baseline tests.

Line coverage cannot distinguish creating a one-line lambda from calling it.
This trace records calls, and never reads patches or failure labels.
"""
import argparse
from collections import defaultdict
import gzip
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("/src"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    os.chdir(source)
    sys.path.insert(0, str(source))
    calls = defaultdict(set)
    items = []
    errors = []

    def profile(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "<lambda>":
            path = Path(frame.f_code.co_filename)
            if path.is_relative_to(source):
                calls[path.relative_to(source).as_posix()].add(frame.f_code.co_firstlineno)

    class Collector:
        def pytest_collection_modifyitems(self, session, config, items):
            self.items = [item.nodeid for item in items]

        def pytest_collectreport(self, report):
            if report.failed:
                errors.append(report.nodeid)

    collector = Collector()
    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        import pytest
        code = pytest.main(["tests", "--collect-only", "-q", "-p", "no:cacheprovider"],
                           plugins=[collector])
        items = sorted(getattr(collector, "items", []))
    finally:
        sys.setprofile(previous)
    result = {"source_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
              "ids": items, "complete": code == 0 and not errors, "failures": errors,
              "lambda_bodies": {path: sorted(lines) for path, lines in sorted(calls.items())},
              "provenance": "Actual Python lambda call events during unchanged pytest collection"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(gzip.compress(json.dumps(result, separators=(",", ":")).encode(), mtime=0))
    print(json.dumps({"complete": result["complete"], "tests": len(items),
                      "lambda_calls": sum(map(len, calls.values())), "failures": errors}))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
