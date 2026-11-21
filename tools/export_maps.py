"""Compress baseline observations into portable test-identity bitsets."""
from __future__ import annotations
import argparse
import collections
import gzip
import hashlib
import json
from pathlib import Path

import coverage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, default=Path("/maps"))
    parser.add_argument("--source", type=Path, default=Path("/src"))
    parser.add_argument("--output", type=Path, default=Path("/entry/artifacts/baseline-map.json.gz"))
    args = parser.parse_args()
    observations = json.loads((args.raw / "observations.json").read_text())
    ids = sorted(observations["items"])
    positions = {name: i for i, name in enumerate(ids)}
    class_bits = collections.defaultdict(int)
    for name, index in positions.items():
        class_bits[name.rsplit("::", 1)[0]] |= 1 << index

    def bits(contexts):
        value = 0
        for context in contexts:
            if context in positions:
                value |= 1 << positions[context]
            elif context.startswith("@class:"):
                value |= class_bits.get(context[len("@class:"):], 0)
        return value

    data = coverage.CoverageData(basename=str(args.raw / ".coverage"))
    data.read()
    lines = {}
    collection = {}
    module_bits = collections.defaultdict(int)
    for filename in sorted(data.measured_files()):
        try:
            relative = Path(filename).relative_to(args.source).as_posix()
        except ValueError:
            continue
        by_line = data.contexts_by_lineno(filename)
        mapped = {str(n): format(bits(c), "x") for n, c in by_line.items() if bits(c)}
        lines[relative] = mapped
        collection[relative] = sorted(n for n, c in by_line.items() if "@collection" in c)
        for value in mapped.values():
            module_bits[relative] |= int(value, 16)
    for file, contexts in observations["modules"].items():
        module_bits[file] |= bits(contexts)
    reads = {file: format(bits(contexts), "x")
             for file, contexts in observations["reads"].items() if bits(contexts)}
    symbols = {symbol: format(bits(contexts), "x")
               for symbol, contexts in observations["symbols"].items() if bits(contexts)}
    attributes = {symbol: format(bits(contexts), "x")
                  for symbol, contexts in observations.get("attributes", {}).items() if bits(contexts)}
    collection_attributes = sorted(k for k, contexts in observations.get("attributes", {}).items()
                                   if "@collection" in contexts)
    source_hashes = {}
    for base in ("sqlglot", "tests"):
        for path in sorted((args.source / base).rglob("*.py")):
            content = path.read_text(encoding="utf-8").replace("\r\n", "\n")
            source_hashes[path.relative_to(args.source).as_posix()] = hashlib.sha256(content.encode()).hexdigest()
    output = {
        "format_version": 1,
        "source_commit": observations["source_commit"],
        "ids": ids, "lines": lines, "collection_lines": collection,
        "modules": {k: format(v, "x") for k, v in module_bits.items()},
        "reads": reads, "symbols": symbols, "source_hashes": source_hashes,
        "attributes": attributes, "collection_attributes": collection_attributes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(output, separators=(",", ":"), sort_keys=True).encode()
    args.output.write_bytes(gzip.compress(payload, compresslevel=9, mtime=0))
    failures = [r for r in observations["results"] if r["outcome"] == "failed"]
    summary = {"source_commit": observations["source_commit"], "collected_tests": len(ids),
               "covered_files": len(lines), "covered_lines": sum(map(len, lines.values())),
               "read_dependencies": len(reads), "symbols": len(symbols), "attributes": len(attributes),
               "instrumented_failures": failures, "compressed_bytes": args.output.stat().st_size,
               "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
               "generation": "Unmodified pinned source only; per-test coverage, process-pool tasks including pickle round trips, file reads, expression/dialect usage, optional observed class-attribute reads"}
    args.output.with_name("provenance.json").write_text(json.dumps(summary, indent=2)+"\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
