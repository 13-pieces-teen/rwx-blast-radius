"""Union raw operand profiles from complete untouched baseline runs."""
import argparse
import copy
import gzip
import hashlib
import json
from pathlib import Path

try:
    from comparison_values import OPERATIONS, decode, encode
except ModuleNotFoundError:
    from tools.comparison_values import OPERATIONS, decode, encode

MODES = {"warm", "cold", "eager-import-collection"}


def merge_profiles(profiles):
    if not profiles or {p.get("mode") for p in profiles} != MODES:
        raise ValueError("complete warm, cold and eager-import traces required")
    headers = ("source_commit", "source_hashes", "generator_hashes", "ids")
    result = {k: copy.deepcopy(profiles[0][k]) for k in headers}
    result.update(complete=True, failures=[], modes=sorted(MODES), sites={},
                  provenance="Union of raw baseline builtin comparison operands; no mutation outcomes")
    if not result["ids"] or len(result["ids"]) != len(set(result["ids"])):
        raise ValueError("nonempty unique test identities required")
    if not result["source_hashes"] or not result["generator_hashes"]:
        raise ValueError("source and observer hashes required")
    observations = {}
    for profile in profiles:
        if profile.get("complete") is not True or profile.get("failures"):
            raise ValueError("failed or incomplete trace")
        if any(profile[k] != result[k] for k in headers):
            raise ValueError("profile source, observers or test identities differ")
        for site, record in profile["sites"].items():
            operation = record["operation"]
            if operation not in OPERATIONS or type(record["opaque"]) is not bool:
                raise ValueError("invalid comparison record")
            target = result["sites"].setdefault(site, {"operation": operation, "opaque": False})
            if target["operation"] != operation:
                raise ValueError("comparison operation differs between traces")
            target["opaque"] |= record["opaque"]
            values = observations.setdefault(site, set())
            for a, b, expected in record["observations"]:
                left, right = decode(a), decode(b)
                if type(expected) is not bool or OPERATIONS[operation](left, right) is not expected:
                    raise ValueError("baseline comparison value/result mismatch")
                values.add((encode(left), encode(right), expected))
    for site, record in result["sites"].items():
        record["observations"] = [] if record["opaque"] else sorted(observations[site])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    blobs = [p.read_bytes() for p in args.inputs]
    result = merge_profiles([json.loads(gzip.decompress(blob)) for blob in blobs])
    result["input_sha256"] = [hashlib.sha256(blob).hexdigest() for blob in blobs]
    result["merge_generator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = gzip.compress(json.dumps(result, separators=(",", ":"), sort_keys=True).encode(), mtime=0)
    args.output.write_bytes(payload)
    summary = {"source_commit": result["source_commit"], "tests": len(result["ids"]),
               "modes": result["modes"], "input_sha256": result["input_sha256"],
               "generator_hashes": result["generator_hashes"],
               "merge_generator_sha256": result["merge_generator_sha256"],
               "sites": len(result["sites"]), "precise": sum(not r["opaque"] for r in result["sites"].values()),
               "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
               "evidence": result["provenance"]}
    args.output.with_name("comparison-provenance.json").write_text(json.dumps(summary, indent=2)+"\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
