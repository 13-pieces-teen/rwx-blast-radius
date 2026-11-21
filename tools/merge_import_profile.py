"""Supplement validated test maps with baseline eager-import observations."""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
from pathlib import Path
import shutil


def read(path):
    return json.loads(gzip.decompress(path.read_bytes()))


def write(path, payload):
    path.write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(), mtime=0))


def validate(base, supplemental, source_hashes=False):
    if not supplemental.get("complete") or supplemental.get("failures"):
        raise ValueError("incomplete or failed supplemental profile")
    for field in ("source_commit", "ids", *(('source_hashes',) if source_hashes else ())):
        if base[field] != supplemental[field]:
            raise ValueError(f"profile {field} differs from baseline")


def merge_import_profile(base, profile, collection=None, class_dicts=None):
    validate(base, profile, source_hashes=True)
    result = copy.deepcopy(base)
    for category in ("collection_attributes", "collection_reads"):
        result[category] = sorted(set(base.get(category, [])) | set(profile[category]))
    for path, lines in profile["collection_lines"].items():
        target = result.setdefault("collection_lines", {})
        target[path] = sorted(set(target.get(path, [])) | set(lines))
    for path, lines in profile["lambda_bodies"].items():
        target = result.setdefault("collection_lambda_bodies", {})
        target[path] = sorted(set(target.get(path, [])) | set(lines))

    minimal = {"source_commit": base["source_commit"], "ids": base["ids"],
               "complete": True, "failures": []}
    if collection is not None:
        validate(base, collection)
        collection = copy.deepcopy(collection)
    else:
        collection = {**minimal, "lambda_bodies": {}}
    for path, lines in result.get("collection_lambda_bodies", {}).items():
        target = collection.setdefault("lambda_bodies", {})
        target[path] = sorted(set(target.get(path, [])) | set(lines))
    collection["eager_imports_included"] = True

    if class_dicts is not None:
        validate(base, class_dicts)
        class_dicts = copy.deepcopy(class_dicts)
    else:
        class_dicts = {**minimal, "readers": {}, "collection": []}
    class_dicts["collection"] = sorted(set(class_dicts.get("collection", [])) | set(profile["collection_class_dict"]))
    class_dicts["eager_imports_included"] = True
    return result, collection, class_dicts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, type=Path, help="Directory containing baseline map and optional profiles")
    p.add_argument("--profile", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    if args.input.resolve() == args.output.resolve():
        raise ValueError("write to a separate candidate directory")
    base_path = args.input / "baseline-map.json.gz"
    collection_path = args.input / "collection-profile.json.gz"
    dict_path = args.input / "class-dict-readers.json.gz"
    profile = read(args.profile)
    merged = merge_import_profile(read(base_path), profile,
                                  read(collection_path) if collection_path.exists() else None,
                                  read(dict_path) if dict_path.exists() else None)
    args.output.mkdir(parents=True, exist_ok=True)
    for path in args.input.glob("*.json*"):
        shutil.copy2(path, args.output / path.name)
    for name, payload in zip(("baseline-map.json.gz", "collection-profile.json.gz", "class-dict-readers.json.gz"), merged):
        write(args.output / name, payload)
    provenance = {
        "source_commit": merged[0]["source_commit"], "collected_tests": len(merged[0]["ids"]),
        "baseline_map_sha256": hashlib.sha256(base_path.read_bytes()).hexdigest(),
        "import_profile_sha256": hashlib.sha256(args.profile.read_bytes()).hexdigest(),
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "input_profiles": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in (collection_path, dict_path) if p.exists()},
        "outputs": {name: hashlib.sha256((args.output/name).read_bytes()).hexdigest()
                    for name in ("baseline-map.json.gz", "collection-profile.json.gz", "class-dict-readers.json.gz")},
        "collection_attributes": len(merged[0]["collection_attributes"]),
        "collection_class_dicts": len(merged[2]["collection"]),
        "import_profile_mode": profile["mode"],
        "evidence": "Baseline eager imports and collect-only succeeded; existing per-test dependencies preserved. This supplement is not a full-suite execution.",
    }
    (args.output / "import-provenance.json").write_text(json.dumps(provenance, indent=2)+"\n")
    print(json.dumps(provenance, indent=2))


if __name__ == "__main__":
    main()
