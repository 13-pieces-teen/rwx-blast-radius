"""Run a Docker submission with official restrictions and score outside the container."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from score import evaluate_run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--data", type=Path, default=ROOT / "sample")
    parser.add_argument("--label", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--entry-script", default="/app/selector.py")
    args = parser.parse_args()
    data = args.data.resolve()
    tests = json.loads((data / "tests.json").read_text())["tests"]
    answers = json.loads((data / "answers.json").read_text())["commits"]
    dest = ROOT / "evaluation" / args.label
    dest.mkdir(parents=True, exist_ok=True)
    reports = []
    image_id = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", args.image], text=True).strip()
    for index in range(args.runs):
        command = [
            "docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
            "--cpus", "2", "--memory", "4096m", "--memory-swap", "4096m",
            "--pids-limit", "256", "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "65534:65534", "--env", "HOME=/tmp",
            "--volume", f"{data / 'commits'}:/data/commits:ro",
            "--volume", f"{data / 'tests.json'}:/data/tests.json:ro",
            "--volume", f"{ROOT / 'tools' / 'measure.py'}:/measure.py:ro",
            "--entrypoint", "python", image_id, "/measure.py", args.entry_script,
        ]
        completed = subprocess.run(command, capture_output=True, timeout=args.timeout)
        stdout, stderr = completed.stdout, completed.stderr.decode("utf-8", errors="replace")
        (dest / f"run-{index + 1}.jsonl").write_bytes(stdout)
        (dest / f"run-{index + 1}.stderr.txt").write_text(stderr, encoding="utf-8")
        if completed.returncode:
            raise RuntimeError(f"Container exited {completed.returncode}: {stderr}")
        if len(stdout) > 2 * 1024 * 1024:
            raise RuntimeError("stdout exceeded 2 MiB")
        evaluation = evaluate_run(stdout, tests, answers)
        if not evaluation["ok"]:
            raise RuntimeError(evaluation)
        metric_lines = [s for s in stderr.splitlines() if s.startswith("RWX_METRICS ")]
        metrics = json.loads(metric_lines[-1].split(" ", 1)[1])
        evaluation.update(metrics)
        evaluation["total_seconds"] = evaluation["simulated_milliseconds"] / 1000 + metrics["cpu_seconds"]
        reports.append(evaluation)
        print(f"Run {index + 1}: {evaluation}", flush=True)
    result = {"image": args.image, "image_id": image_id, "entry_script": args.entry_script, "data": str(data), "runs": reports,
              "median_total_seconds": statistics.median(x["total_seconds"] for x in reports),
              "cpu_measurement": "Linux process user+system CPU, including child processes"}
    (dest / "report.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"label": args.label, "median_total_seconds": result["median_total_seconds"]}))


if __name__ == "__main__":
    main()
