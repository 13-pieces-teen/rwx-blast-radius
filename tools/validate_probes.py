"""Check all omitted tests on fresh source mutations, outside the submission image."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from selector import Selector, apply_change, parse_patch, schedule


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, default=Path("/src"))
    p.add_argument("--probes", type=Path, default=ROOT / "evaluation/probes")
    p.add_argument("--work", type=Path, default=Path("/tmp/rwx-probe-source"))
    p.add_argument("--limit", type=int)
    p.add_argument("--only", nargs="*")
    p.add_argument("--label", default="default")
    p.add_argument("--maps", type=Path, default=ROOT / "artifacts/baseline-map.json.gz")
    args = p.parse_args()
    data = json.loads((ROOT / "sample/tests.json").read_text())["tests"]
    maps = args.maps
    selector = Selector(args.source, maps, data)
    manifest = json.loads((args.probes / "manifest.json").read_text())
    rows = manifest["mutations"]
    if args.only:
        rows = [r for r in rows if r["id"] in args.only]
    if args.limit:
        rows = rows[:args.limit]
    shutil.copytree(args.source, args.work, ignore=shutil.ignore_patterns(".git", "__pycache__"), dirs_exist_ok=True)
    reports = []
    destination = args.probes / ("validation-" + args.label)
    destination.mkdir(exist_ok=True)
    (destination / "provenance.json").write_text(json.dumps({
        "selector_sha256": hashlib.sha256((ROOT / "selector.py").read_bytes()).hexdigest(),
        "map_sha256": hashlib.sha256(maps.read_bytes()).hexdigest(),
        "profiles_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in sorted(maps.parent.glob("*.json.gz"))},
        "type_profiles_sha256": (hashlib.sha256((maps.parent / "type-profiles.json.gz").read_bytes()).hexdigest()
                                  if (maps.parent / "type-profiles.json.gz").exists() else None),
        "reflection_fields_sha256": (hashlib.sha256((maps.parent / "reflection-fields.json.gz").read_bytes()).hexdigest()
                                     if (maps.parent / "reflection-fields.json.gz").exists() else None),
        "collection_profile_sha256": (hashlib.sha256((maps.parent / "collection-profile.json.gz").read_bytes()).hexdigest()
                                      if (maps.parent / "collection-profile.json.gz").exists() else None),
        "class_dict_readers_sha256": (hashlib.sha256((maps.parent / "class-dict-readers.json.gz").read_bytes()).hexdigest()
                                     if (maps.parent / "class-dict-readers.json.gz").exists() else None),
        "lambda_call_readers_sha256": (hashlib.sha256((maps.parent / "lambda-call-readers.json.gz").read_bytes()).hexdigest()
                                      if (maps.parent / "lambda-call-readers.json.gz").exists() else None),
        "source_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
        "mode": "Execute every omitted test; stop at first counterexample per mutation",
    }, indent=2) + "\n")
    for row in rows:
        patch = (args.probes / "commits" / (row["id"] + ".patch")).read_text()
        selected = selector.select(patch)
        selected_numbers = {t["number"] for t in selected}
        omitted = [t["id"] for t in data if t["number"] not in selected_numbers]
        files = {}
        for change in parse_patch(patch):
            target = args.work / change.old
            original = target.read_text(encoding="utf-8")
            modified, _, _ = apply_change(original, change)
            files[target] = original
            target.write_text(modified, encoding="utf-8")
        result_path = destination / (row["id"] + ".json")
        config_path = destination / (row["id"] + ".config.json")
        config_path.write_text(json.dumps({"source": str(args.work), "include": omitted,
                                          "result": str(result_path)}))
        started = time.monotonic()
        try:
            if omitted:
                env = dict(os.environ, PYTHONPATH=str(args.work), PYTHON_CPU_COUNT="2", PYTHONDONTWRITEBYTECODE="1",
                           SKIP_INTEGRATION="0", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
                with (destination / (row["id"] + ".log")).open("wb") as log:
                    completed = subprocess.run([sys.executable, str(ROOT / "tools/run_probe_tests.py"), str(config_path)],
                                               env=env, stdout=log, stderr=subprocess.STDOUT, timeout=240)
                result = json.loads(result_path.read_text()) if result_path.exists() else {"runner_exit": completed.returncode}
            else:
                result = {"exit_code": 0, "failed": [], "passed": 0, "skipped": [], "collection_errors": []}
        except subprocess.TimeoutExpired:
            result = {"timeout": True}
        finally:
            for target, original in files.items():
                target.write_text(original, encoding="utf-8")
        result.update({"id": row["id"], "selected": len(selected), "omitted": len(omitted),
                       "wall_seconds": time.monotonic() - started})
        reports.append(result)
        (destination / "report.json").write_text(json.dumps(reports, indent=2) + "\n")
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
