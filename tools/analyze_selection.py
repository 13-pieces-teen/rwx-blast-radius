"""Offline diagnostics only; sample answers are never sent to the selector image."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from selector import Selector, schedule
from score import evaluate_run


def cost(workers, tests):
    duration = {t["number"]: int(t["seconds"] * 1000 + .5) for t in tests}
    return max((sum(duration[n] for n in w) for w in workers), default=0) + len(workers) * 2000


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--data", type=Path, default=ROOT / "sample")
    p.add_argument("--label", default="selection")
    p.add_argument("--maps", type=Path, default=ROOT / "artifacts/baseline-map.json.gz")
    args = p.parse_args()
    tests = json.loads((args.data / "tests.json").read_text())["tests"]
    answers = json.loads((args.data / "answers.json").read_text())["commits"]
    started = time.process_time()
    selector = Selector(args.source, args.maps, tests)
    outputs, rows = [], []
    for patch in sorted((args.data / "commits").glob("*.patch")):
        selected = selector.select(patch.read_text())
        workers = schedule(selected)
        output = {"commit": patch.stem, "workers": workers}
        outputs.append(json.dumps(output))
        numbers = {t["number"] for t in selected}
        failing = set(answers[patch.stem]["failing"])
        row = {"commit": patch.stem, "selected": len(numbers), "failing": len(failing),
               "missed": sorted(failing - numbers), "worker_count": len(workers),
               "cost_seconds": cost(workers, tests) / 1000,
               "selected_test_seconds": sum(t["seconds"] for t in selected),
               "oracle_cost_seconds": cost(schedule([t for t in tests if t["number"] in failing]), tests) / 1000}
        rows.append(row)
        print(json.dumps(row), flush=True)
    elapsed = time.process_time() - started
    output = ("\n".join(outputs) + "\n").encode()
    evaluation = evaluate_run(output, tests, answers)
    report = {"evaluation": evaluation, "host_cpu_seconds": elapsed,
              "oracle_simulated_seconds": sum(r["oracle_cost_seconds"] for r in rows), "rows": rows}
    dest = ROOT / "evaluation" / args.label
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (dest / "output.jsonl").write_bytes(output)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    main()
