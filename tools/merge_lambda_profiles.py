"""Union complete warm/cold lambda call traces and eager import calls."""
import argparse
import copy
import gzip
import hashlib
import json
from pathlib import Path


def merge_profiles(profiles):
    if not profiles:
        raise ValueError("at least one trace is required")
    modes = {profile.get("mode") for profile in profiles}
    if not {"warm", "cold", "eager-import-collection"} <= modes:
        raise ValueError("warm, cold, and eager-import traces are required")
    first = profiles[0]
    result = {key: copy.deepcopy(first[key]) for key in ("source_commit", "ids", "source_hashes")}
    result.update({"complete": True, "failures": [], "readers": {}, "collection": [],
                   "modes": sorted(modes), "runs": [],
                   "provenance": "Union of complete warm/cold baseline test runs plus eager-import collection; actual lambda body calls"})
    collection = set()
    sites = set(first["readers"])
    for profile in profiles:
        if not profile.get("complete") or profile.get("failures"):
            raise ValueError("incomplete or failed lambda trace")
        for key in ("source_commit", "ids", "source_hashes"):
            if profile[key] != result[key]:
                raise ValueError(f"lambda trace {key} differs")
        if set(profile["readers"]) != sites:
            raise ValueError("lambda site catalog differs")
        for site, readers in profile["readers"].items():
            result["readers"][site] = format(int(result["readers"].get(site, "0"), 16) | int(readers, 16), "x")
        collection.update(profile["collection"])
        result["runs"].append({"mode": profile["mode"], "complete": profile["complete"],
                               "collected_tests": len(profile["ids"]), "test_counts": profile.get("test_counts", {}),
                               "generator_hashes": profile.get("generator_hashes", {})})
    if not collection <= sites:
        raise ValueError("collection call is not in the baseline site catalog")
    result["collection"] = sorted(collection)
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("inputs", nargs="+", type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    blobs = [path.read_bytes() for path in args.inputs]
    result = merge_profiles([json.loads(gzip.decompress(blob)) for blob in blobs])
    result["input_sha256"] = [hashlib.sha256(blob).hexdigest() for blob in blobs]
    result["merge_generator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    blob = gzip.compress(json.dumps(result, separators=(",", ":"), sort_keys=True).encode(), mtime=0)
    args.output.write_bytes(blob)
    summary = {"modes": result["modes"], "tests": len(result["ids"]), "sites": len(result["readers"]),
               "collection": len(result["collection"]), "runs": result["runs"],
               "sha256": hashlib.sha256(blob).hexdigest()}
    args.output.with_name("lambda-provenance.json").write_text(json.dumps(summary, indent=2)+"\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
