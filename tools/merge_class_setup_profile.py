"""Union class fixture observations into a separate baseline-map candidate."""
import argparse
import copy
import gzip
import hashlib
import json
from pathlib import Path
import shutil


def merge(base, profile, class_dicts, lambdas):
    if not profile.get("complete") or profile.get("failures"):
        raise ValueError("incomplete class setup profile")
    for key in ("source_commit", "ids", "source_hashes"):
        if base[key] != profile[key]:
            raise ValueError(f"class setup {key} mismatch")
    result = copy.deepcopy(base)

    def union(target, incoming):
        for key, bits in incoming.items():
            target[key] = format(int(target.get(key, "0"), 16) | int(bits, 16), "x")

    for path, lines in profile["lines"].items():
        union(result["lines"].setdefault(path, {}), lines)
    for category in ("modules", "reads", "symbols"):
        union(result[category], profile[category])
    # New field keys would enable new precise narrowing. Only augment existing
    # field readers; broad class/module edges cover the other declarations.
    union(result["attributes"], {k: v for k, v in profile["attributes"].items() if k in result["attributes"]})
    extras = []
    for original, category in ((class_dicts, "class_dict_reads"), (lambdas, "lambda_call_readers")):
        if (not original.get("complete") or original.get("failures") or
                any(original[key] != base[key] for key in ("source_commit", "ids"))):
            raise ValueError("mismatched supplemental reader profile")
        updated = copy.deepcopy(original)
        union(updated["readers"], profile[category])
        updated["class_setup_included"] = True
        extras.append(updated)
    return result, *extras


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--profile", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.input.resolve() == args.output.resolve():
        raise ValueError("write to a separate candidate directory")
    names = ("baseline-map.json.gz", "class-dict-readers.json.gz", "lambda-call-readers.json.gz")
    read = lambda path: json.loads(gzip.decompress(path.read_bytes()))
    base, dictionaries, lambdas = [read(args.input / name) for name in names]
    profile = read(args.profile)
    result = merge(base, profile, dictionaries, lambdas)
    args.output.mkdir(parents=True, exist_ok=True)
    for file in args.input.glob("*.json*"):
        shutil.copy2(file, args.output / file.name)
    for name, payload in zip(names, result):
        (args.output / name).write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(), mtime=0))
    provenance = {"source_commit": base["source_commit"], "ids": len(base["ids"]),
                  "profile_sha256": hashlib.sha256(args.profile.read_bytes()).hexdigest(),
                  "input_sha256": {n: hashlib.sha256((args.input / n).read_bytes()).hexdigest() for n in names},
                  "outputs": {n: hashlib.sha256((args.output / n).read_bytes()).hexdigest() for n in names},
                  "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  "sentinel_tests": profile["selected_tests"], "evidence": profile["provenance"]}
    (args.output / "class-setup-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps(provenance, indent=2))


if __name__ == "__main__":
    main()
