"""Union independent baseline observations without conflating runtime type identities."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path


def merge_profiles(profiles):
    if not profiles:
        raise ValueError("at least one profile is required")
    first = profiles[0]
    result = {"source_commit": first["source_commit"], "ids": first["ids"],
              "complete": True, "failures": [], "types": [], "comparisons": {},
              "cache_modes": [],
              "provenance": "Union of untouched pinned-source observations; independent type identity namespaces"}
    identity_offset = 0
    for profile in profiles:
        if not profile.get("complete") or profile.get("failures"):
            raise ValueError("incomplete or failed profile")
        if profile["source_commit"] != result["source_commit"] or profile["ids"] != result["ids"]:
            raise ValueError("profile source or test identities differ")
        index_offset = len(result["types"])
        for info in profile["types"]:
            result["types"].append({**info, "type_identity": info["type_identity"] + identity_offset})
        # Comparisons never combine operands from different tracing runs. Giving
        # each run its own type identities preserves equality within a pair and
        # prevents accidentally identifying unrelated classes across processes.
        identity_offset += max((info["type_identity"] for info in profile["types"]), default=-1) + 1
        for site, observations in profile["comparisons"].items():
            merged = result["comparisons"].setdefault(site, [])
            for left, right, left_member, right_member in observations:
                if not (0 <= left < len(profile["types"]) and 0 <= right < len(profile["types"])):
                    raise ValueError("observation references an invalid type")
                merged.append([left + index_offset, right + index_offset, left_member, right_member])
        result["cache_modes"].extend(profile.get("cache_modes", ["cold" if profile.get("cold_caches") else "warm"]))
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("inputs", nargs="+", type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    blobs = [path.read_bytes() for path in args.inputs]
    merged = merge_profiles([json.loads(gzip.decompress(blob)) for blob in blobs])
    merged["input_sha256"] = [hashlib.sha256(blob).hexdigest() for blob in blobs]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    data = gzip.compress(json.dumps(merged, separators=(",", ":")).encode(), mtime=0)
    args.output.write_bytes(data)
    print(json.dumps({"sites": len(merged["comparisons"]), "types": len(merged["types"]),
                      "cache_modes": merged["cache_modes"], "sha256": hashlib.sha256(data).hexdigest()}))


if __name__ == "__main__":
    main()
