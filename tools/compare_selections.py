"""Offline coverage-regression audit; never part of the submission image."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from selector import Selector


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--old-ref", default="5887d18")
    parser.add_argument("--prepared", type=Path, help="Historical selector.py and artifacts, prepared outside Docker")
    parser.add_argument("--maps", type=Path, default=ROOT / "artifacts/baseline-map.json.gz")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    old_artifacts = args.prepared / "artifacts" if args.prepared else args.output / "old-artifacts"
    old_artifacts.mkdir(exist_ok=True)
    if args.prepared:
        code = (args.prepared / "selector.py").read_bytes()
    else:
        for name in ("baseline-map.json.gz", "type-profiles.json.gz", "reflection-fields.json.gz"):
            payload = subprocess.check_output(["git", "show", f"{args.old_ref}:artifacts/{name}"], cwd=ROOT)
            (old_artifacts / name).write_bytes(payload)
        code = subprocess.check_output(["git", "show", f"{args.old_ref}:selector.py"], cwd=ROOT)
    spec = importlib.util.spec_from_loader("old_selector", loader=None)
    old_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = old_module
    # Preserve the import-check helper's path while loading the historical code.
    old_module.__file__ = str(ROOT / "selector.py")
    exec(compile(code, old_module.__file__, "exec"), old_module.__dict__)
    tests = json.loads((ROOT / "sample/tests.json").read_text())["tests"]
    old = old_module.Selector(args.source, old_artifacts / "baseline-map.json.gz", tests)
    new = Selector(args.source, args.maps, tests)
    paths = sorted(set((ROOT / "sample/commits").glob("*.patch")) |
                   set((ROOT / "evaluation").glob("**/commits/*.patch")))
    rows = []
    for path in paths:
        patch = path.read_text(encoding="utf-8")
        before = {t["id"] for t in old.select(patch)}
        after = {t["id"] for t in new.select(patch)}
        rows.append({"patch": path.relative_to(ROOT).as_posix(), "old": len(before),
                     "new": len(after), "added": sorted(after - before),
                     "removed": sorted(before - after)})
    result = {"patches": len(rows), "narrowed": sum(bool(r["removed"]) for r in rows),
              "selector_sha256": hashlib.sha256((ROOT / "selector.py").read_bytes()).hexdigest(),
              "map_sha256": hashlib.sha256(args.maps.read_bytes()).hexdigest(),
              "profile_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in sorted(args.maps.parent.glob("*.json.gz"))},
              "rows": rows}
    (args.output / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}))


if __name__ == "__main__":
    main()
