"""Seal two independently captured pristine import snapshots into one profile."""
from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import json
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stable(snapshot):
    return {key: snapshot[key] for key in ("schema_version", "fields", "classes", "aliases", "unsupported_fields", "normalization_policy")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--baseline-map", type=Path, required=True)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    args = parser.parse_args()
    first_path, second_path = (args.run / "output" / f"baseline-{i}.json.gz" for i in (1, 2))
    first, second = (json.loads(gzip.decompress(path.read_bytes())) for path in (first_path, second_path))
    baseline = json.loads(gzip.decompress(args.baseline_map.read_bytes()))
    freeze = json.loads((args.run / "freeze.json").read_text())
    inspect = json.loads((args.run / "inspect.json").read_text())[0]
    assert freeze["pristine_only"] is True and freeze["PYTHONHASHSEED"] == "0"
    assert freeze["runner_hashes"]["import_state.py"] == sha(args.helper)
    assert inspect["Image"] == freeze["image"]
    assert inspect["State"]["ExitCode"] == 0 and not inspect["State"]["OOMKilled"]
    assert json.loads((args.run / "attach.json").read_text())["returncode"] == 0
    assert json.loads((args.run / "cleanup.json").read_text())["returncode"] == 0
    expected_hashes = {key: value for key, value in baseline["source_hashes"].items() if key.startswith("sqlglot/")}
    assert first["source_hashes"] == second["source_hashes"] == expected_hashes
    assert first["imports"] == second["imports"]
    assert stable(first) == stable(second), "fresh-process state is not stable"
    semantic = stable(first)
    semantic_hash = hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    profile = {"schema_version": 1, "source_commit": baseline["source_commit"], "source_hashes": expected_hashes,
        "imports": first["imports"], "pythonhashseed": "0", "helper_sha256": sha(args.helper),
        "snapshot": semantic, "snapshot_sha256": semantic_hash}
    encoded = gzip.compress(json.dumps(profile, separators=(",", ":")).encode(), mtime=0)
    with args.output.open("xb") as stream:
        stream.write(encoded)
    summary = json.loads((args.run / "output/summary.json").read_text())
    provenance = {"created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "complete": True,
        "baseline_only": True, "source_commit": profile["source_commit"], "source_files": len(expected_hashes),
        "helper_sha256": sha(args.helper), "artifact_sha256": sha(args.output), "artifact_bytes": len(encoded),
        "snapshot_sha256": semantic_hash, "image_id": inspect["Image"], "pythonhashseed": "0",
        "resource_limits": {"cpuset": inspect["HostConfig"]["CpusetCpus"], "nano_cpus": inspect["HostConfig"]["NanoCpus"],
                            "memory": inspect["HostConfig"]["Memory"]}, "fresh_processes": summary["processes"],
        "evidence": {str(p): sha(p) for p in [first_path, second_path, args.run / "freeze.json", args.run / "inspect.json", args.run / "cleanup.json", args.baseline_map]},
        "normalization_contract": ["Class fields are stored once at their defining class; classes.mro and own_fields support inherited reader expansion.",
            "Derived fields, defaults, kwdefaults, closures, expression instance slots, enums and regex state contribute their values.",
            "Declared Python function code is handled by runtime line coverage; container functions, lambdas and generated closures include code semantics.",
            "Same-object field aliases and immediate container-member aliases are recorded without process addresses.",
            "Type-keyed mapping iteration order is canonicalized because class hashes depend on addresses; ordinary dict insertion order is retained.",
            "Source-recognized tuple(set-of-ordinary-types) used only as isinstance/issubclass type sets is sorted; MRO, bases and ordinary tuples retain order.",
            "External modules/functions/classes and interpreter loader/synchronization noise use stable symbolic identity.",
            "Unknown values are recorded on their containing fields, never a global opaque veto.",
            "Snapshot equality is a field dependency observation, not a proof of arbitrary filesystem, network or interpreter side-effect equivalence."]}
    with args.provenance.open("x", encoding="utf8") as stream:
        json.dump(provenance, stream, indent=2); stream.write("\n")
    print(json.dumps({"artifact": str(args.output), "bytes": len(encoded), "sha256": sha(args.output), "helper_sha256": sha(args.helper), "snapshot_sha256": semantic_hash}))


if __name__ == "__main__":
    main()
