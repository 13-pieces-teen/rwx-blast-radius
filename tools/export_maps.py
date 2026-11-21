"""Compress baseline observations into portable test-identity bitsets."""
from __future__ import annotations
import argparse
import collections
import gzip
import hashlib
import json
from pathlib import Path


def validate_observations(observations):
    failures = [r for r in observations["results"] if r["outcome"] == "failed"]
    if failures:
        raise ValueError(f"refusing failed baseline observations: {len(failures)} reports")
    completed = {r["id"] for r in observations["results"]
                 if (r["when"] == "call" and r["outcome"] in ("passed", "skipped"))
                 or (r["when"] == "setup" and r["outcome"] == "skipped")}
    if not observations["items"] or completed != set(observations["items"]):
        raise ValueError("refusing incomplete baseline observations")
    for phase in ("setup", "teardown"):
        ended = {r["id"] for r in observations["results"]
                 if r["when"] == phase and r["outcome"] in ("passed", "skipped")}
        if ended != set(observations["items"]):
            raise ValueError(f"refusing baseline observations missing {phase}")


def validate_coverage(directory, observations):
    import coverage
    path = directory / ".coverage"
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("missing baseline coverage database")
    data = coverage.CoverageData(basename=str(path))
    data.read()
    passed = {r["id"] for r in observations["results"]
              if r["when"] == "call" and r["outcome"] == "passed"}
    if not data.measured_files() or not passed <= data.measured_contexts():
        raise ValueError("baseline coverage is missing completed test contexts")
    return data


def union_maps(current, previous, attribute_keys=None):
    """Keep both cold-cache and warm-cache observations without losing edges."""
    for key in ("source_commit", "ids", "source_hashes"):
        if current[key] != previous[key]:
            raise ValueError(f"cannot union maps with different {key}")
    for category in ("modules", "reads", "symbols", "attributes"):
        for key, value in previous.get(category, {}).items():
            current[category][key] = format(int(current[category].get(key, "0"), 16) | int(value, 16), "x")
    for path, lines in previous["lines"].items():
        target = current["lines"].setdefault(path, {})
        for line, value in lines.items():
            target[line] = format(int(target.get(line, "0"), 16) | int(value, 16), "x")
    for path, lines in previous.get("collection_lines", {}).items():
        current["collection_lines"][path] = sorted(set(current["collection_lines"].get(path, [])) | set(lines))
    for category in ("collection_attributes", "collection_reads"):
        current[category] = sorted(set(current.get(category, [])) | set(previous.get(category, [])))
    if attribute_keys is not None:
        current["attributes"] = {k: v for k, v in current["attributes"].items() if k in attribute_keys}


def main():
    import coverage

    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, default=Path("/maps"))
    parser.add_argument("--source", type=Path, default=Path("/src"))
    parser.add_argument("--output", type=Path, default=Path("/entry/artifacts/baseline-map.json.gz"))
    parser.add_argument("--union", type=Path, action="append", default=[])
    parser.add_argument("--attribute-keys", type=Path,
                        help="Only enable precise field selection for keys already in this map")
    args = parser.parse_args()
    observations = json.loads((args.raw / "observations.json").read_text())
    validate_observations(observations)
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

    data = validate_coverage(args.raw, observations)
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
        "collection_reads": sorted(file for file, contexts in observations["reads"].items()
                                   if "@collection" in contexts and
                                   (observations.get("collection_reads_clean", False) or not file.endswith(".py"))),
    }
    union_inputs = []
    attribute_keys = (set(json.loads(gzip.decompress(args.attribute_keys.read_bytes()))["attributes"])
                      if args.attribute_keys else None)
    for path in args.union:
        payload = path.read_bytes()
        union_maps(output, json.loads(gzip.decompress(payload)), attribute_keys)
        input_info = {"sha256": hashlib.sha256(payload).hexdigest()}
        sidecar = path.with_name("provenance.json")
        if sidecar.exists():
            provenance = json.loads(sidecar.read_text())
            if provenance.get("sha256") != input_info["sha256"]:
                raise ValueError("union input provenance hash mismatch")
            input_info["provenance"] = provenance
        union_inputs.append(input_info)
    if attribute_keys is not None:
        output["attributes"] = {k: v for k, v in output["attributes"].items() if k in attribute_keys}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(output, separators=(",", ":"), sort_keys=True).encode()
    args.output.write_bytes(gzip.compress(payload, compresslevel=9, mtime=0))
    failures = [r for r in observations["results"] if r["outcome"] == "failed"]
    summary = {"source_commit": observations["source_commit"], "collected_tests": len(ids),
               "covered_files": len(output["lines"]), "covered_lines": sum(map(len, output["lines"].values())),
               "read_dependencies": len(output["reads"]), "symbols": len(output["symbols"]), "attributes": len(output["attributes"]),
               "cold_caches": observations.get("cold_caches", False), "union_inputs": union_inputs,
               "generator_hashes": observations.get("generator_hashes", {}),
               "recoveries": observations.get("recoveries", []),
               "collection_reads_note": (
                   "Observer field scan precedes audit installation"
                   if observations.get("collection_reads_clean", False) else
                   "Legacy observer scanned Python sources with audit enabled; Python collection reads excluded. Use the clean eager-import profile for these dependencies."),
               "restricted_attribute_keys": args.attribute_keys is not None,
               "instrumented_failures": failures, "compressed_bytes": args.output.stat().st_size,
               "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
               "generation": "Unmodified pinned source only; per-test coverage, process-pool tasks including pickle round trips, file reads, expression/dialect usage, optional observed class-attribute reads"}
    args.output.with_name("provenance.json").write_text(json.dumps(summary, indent=2)+"\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
