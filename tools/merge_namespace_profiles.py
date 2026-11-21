"""Union trustworthy warm/cold/eager class namespace traces."""
import argparse
import copy
import gzip
import hashlib
import json
from pathlib import Path


MODES = {"warm", "cold", "eager-import-collection"}
CATEGORIES = ("all", "opaque", "fields", "keys")


def profile_version(profile):
    version = profile.get("format_version", 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError("unsupported namespace profile format")
    if version == 1 and "keys" in profile:
        raise ValueError("keys-only reads require namespace format 2")
    return version


def categories(profile):
    return CATEGORIES if profile_version(profile) == 2 else CATEGORIES[:-1]


def field_owner(key):
    parts = key.split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise ValueError("invalid namespace field key")
    return ":".join(parts[:2])


def validate_profile(profile):
    profile_version(profile)
    if profile.get("complete") is not True or profile.get("failures"):
        raise ValueError("incomplete or failed namespace trace")
    if profile.get("mode") not in MODES:
        raise ValueError("unknown namespace trace mode")
    ids = profile.get("ids", [])
    if not ids or any(not isinstance(item, str) for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("namespace trace requires unique nonempty test identities")
    for name in ("source_hashes", "generator_hashes"):
        if not isinstance(profile.get(name), dict) or not profile[name]:
            raise ValueError(f"missing namespace trace {name}")
    limit = (1 << len(ids)) - 1
    parsed = {}
    collections = {}
    for category in categories(profile):
        records = profile.get(category)
        if (not isinstance(records, dict) or not isinstance(records.get("readers"), dict)
                or not isinstance(records.get("collection"), list)
                or any(not isinstance(key, str) for key in records["collection"])
                or len(records["collection"]) != len(set(records["collection"]))):
            raise ValueError("missing or invalid namespace category")
        parsed[category] = {}
        for key, value in records["readers"].items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("invalid namespace reader encoding")
            try:
                bits = int(value, 16)
            except ValueError as exc:
                raise ValueError("invalid namespace reader bitset") from exc
            if bits < 0 or bits & ~limit:
                raise ValueError("namespace reader bitset exceeds collected test identities")
            parsed[category][key] = bits
        collections[category] = set(records["collection"])
        if not collections[category] <= parsed[category].keys():
            raise ValueError("collection namespace read is absent from its reader catalog")
    classified = dict(parsed["opaque"])
    classified_collection = set(collections["opaque"])
    classified_symbols = set(classified)
    for key, bits in parsed.get("keys", {}).items():
        classified_symbols.add(key)
        classified[key] = classified.get(key, 0) | bits
    classified_collection.update(collections.get("keys", ()))
    for key, bits in parsed["fields"].items():
        owner = field_owner(key)
        classified_symbols.add(owner)
        classified[owner] = classified.get(owner, 0) | bits
        if key in collections["fields"]:
            classified_collection.add(owner)
    if classified_symbols != set(parsed["all"]):
        raise ValueError("namespace events do not match the all-read class catalog")
    if classified != parsed["all"] or classified_collection != collections["all"]:
        raise ValueError("namespace events are missing or inconsistently classified")


def merge_profiles(profiles):
    if not profiles:
        raise ValueError("namespace traces are required")
    for profile in profiles:
        validate_profile(profile)
    modes = {profile["mode"] for profile in profiles}
    if modes != MODES:
        raise ValueError("complete warm, cold, and eager-import traces are required")
    first = profiles[0]
    version = profile_version(first)
    if any(profile_version(profile) != version for profile in profiles):
        raise ValueError("namespace trace format differs")
    headers = ("source_commit", "ids", "source_hashes", "generator_hashes")
    result = {key: copy.deepcopy(first[key]) for key in headers}
    result.update(format_version=version, complete=True, failures=[], modes=sorted(modes), runs=[],
                  provenance="Complete warm/cold baseline tests and eager imports; specific values, membership keys and opaque namespace consumers")
    active_categories = categories(first)
    for category in active_categories:
        result[category] = {"readers": {}, "collection": []}
    collections = {category: set() for category in active_categories}
    for profile in profiles:
        for key in headers:
            if profile[key] != result[key]:
                raise ValueError(f"namespace trace {key} differs")
        for category in active_categories:
            readers = result[category]["readers"]
            for key, bits in profile[category]["readers"].items():
                readers[key] = format(int(readers.get(key, "0"), 16) | int(bits, 16), "x")
            collections[category].update(profile[category]["collection"])
        result["runs"].append({"format_version": version, "mode": profile["mode"], "complete": True,
                               "collected_tests": len(profile["ids"]),
                               "generator_hashes": copy.deepcopy(profile["generator_hashes"])})
    for category in active_categories:
        result[category]["collection"] = sorted(collections[category])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    blobs = [path.read_bytes() for path in args.inputs]
    result = merge_profiles([json.loads(gzip.decompress(blob)) for blob in blobs])
    result["input_sha256"] = [hashlib.sha256(blob).hexdigest() for blob in blobs]
    result["merge_generator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    blob = gzip.compress(json.dumps(result, separators=(",", ":"), sort_keys=True).encode(), mtime=0)
    args.output.write_bytes(blob)
    summary = {"format_version": result["format_version"], "modes": result["modes"], "tests": len(result["ids"]), "runs": result["runs"],
               "input_sha256": result["input_sha256"], "merge_generator_sha256": result["merge_generator_sha256"],
               "sites": {category: len(result[category]["readers"]) for category in categories(result)},
               "sha256": hashlib.sha256(blob).hexdigest()}
    args.output.with_name("namespace-provenance.json").write_text(json.dumps(summary, indent=2)+"\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
