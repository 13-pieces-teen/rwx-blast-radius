"""Union a complete isolated retrace after a recorded instrumentation failure.

Original raw traces are immutable. The output retains recovery provenance so a
full-suite failure followed by a passing isolated retry is never presented as
one passing full-suite run.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path

if __package__:
    from .export_maps import validate_observations, validate_coverage
else:
    from export_maps import validate_observations, validate_coverage


def replace_retried_reads(original, retried, retry_ids):
    """Discard failing-test reads before adding its successful retrace.

    Pytest's failure formatter reads source files. Those reads are diagnostics,
    not dependencies of the original passing test. Other test and collection
    contexts are retained; executable coverage and symbol edges are unaffected.
    """
    result = {path: set(contexts) - retry_ids for path, contexts in original.items()}
    removed = [{"path": path, "context": context}
               for path, contexts in original.items()
               for context in sorted((set(contexts) & retry_ids) - set(retried.get(path, [])))]
    for path, contexts in retried.items():
        result.setdefault(path, set()).update(contexts)
    return {path: sorted(contexts) for path, contexts in result.items() if contexts}, removed


def validate_prefix_sources(prefix, manifest, source):
    expected = manifest["source_hashes"]
    if set(prefix["source_hashes"]) != set(expected):
        raise ValueError("prefix source catalog differs from pinned baseline")
    raw_hashes, normalized_hashes = {}, {}
    for relative in expected:
        content = (source / relative).read_bytes()
        raw_hashes[relative] = hashlib.sha256(content).hexdigest()
        normalized_hashes[relative] = hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()
    if prefix["source_hashes"] != raw_hashes or normalized_hashes != expected:
        raise ValueError("prefix source content differs from pinned baseline")
    return "Raw source bytes verified unchanged; CRLF normalized only for comparison with the baseline map manifest"


def main():
    import coverage
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--supplement", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--source", type=Path, default=Path("/src"))
    args = parser.parse_args()
    if args.output.resolve() in (args.base.resolve(), args.supplement.resolve()):
        raise ValueError("recovery must preserve its inputs")
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("recovery output must be new or empty")
    base_bytes = (args.base / "observations.json").read_bytes()
    retry_bytes = (args.supplement / "observations.json").read_bytes()
    base, retry = json.loads(base_bytes), json.loads(retry_bytes)
    prefix_path = args.supplement / "prefix-provenance.json"
    prefix = None
    if prefix_path.exists():
        if __package__:
            from .retrace_prefix import validate_prefix
        else:
            from retrace_prefix import validate_prefix
        prefix = json.loads(prefix_path.read_text())
        if (prefix.get("complete") is not True or
                prefix.get("base_observations_sha256") != hashlib.sha256(base_bytes).hexdigest()):
            raise ValueError("incomplete prefix or original trace mismatch")
        validate_prefix(base, prefix)
        if retry["items"] != [prefix["target"]]:
            raise ValueError("prefix supplement must contain exactly its verified target")
        target_results = [row for row in prefix["prefix_results"] if row["id"] == prefix["target"]]
        if retry.get("generator_hashes") != prefix["generator_hashes"] or retry["results"] != target_results:
            raise ValueError("supplement observer identity/results differ from prefix provenance")
        seal_path = args.supplement / "prefix-seal.json"
        seal = json.loads(seal_path.read_text())
        if (seal.get("complete") is not True or
                seal.get("base_observations_sha256") != hashlib.sha256(base_bytes).hexdigest() or
                any(hashlib.sha256((args.supplement/name).read_bytes()).hexdigest() != seal["sha256"].get(name)
                    for name in ("observations.json", ".coverage", "prefix-provenance.json"))):
            raise ValueError("prefix immutable trace seal mismatch")
        manifest_path = Path(__file__).resolve().parents[1] / "artifacts/baseline-map.json.gz"
        manifest = json.loads(gzip.decompress(manifest_path.read_bytes()))
        if (prefix.get("source_commit") != manifest["source_commit"] or
                sorted(prefix["full_collection"]) != manifest["ids"]):
            raise ValueError("prefix source/test manifest differs from pinned baseline")
        source_verification = validate_prefix_sources(prefix, manifest, args.source)
    validate_observations(retry)
    base_data = validate_coverage(args.base, base)
    retry_data = validate_coverage(args.supplement, retry)
    for key in ("source_commit", "cold_caches"):
        if base.get(key, False) != retry.get(key, False):
            raise ValueError(f"retrace differs in {key}")
    retry_ids = set(retry["items"])
    if not retry_ids <= set(base["items"]):
        raise ValueError("retrace contains tests outside the original collection")
    failed = [r for r in base["results"] if r["outcome"] == "failed"]
    if not failed or not {r["id"] for r in failed} <= retry_ids:
        raise ValueError("every failing test requires a complete passing retrace")
    base["results"] = [r for r in base["results"] if r["id"] not in retry_ids] + retry["results"]
    base["reads"], removed_reads = replace_retried_reads(base["reads"], retry["reads"], retry_ids)
    for category in ("symbols", "attributes", "modules"):
        for key, contexts in retry[category].items():
            base[category][key] = sorted(set(base[category].get(key, [])) | set(contexts))
    base.setdefault("recoveries", []).append({
        "reason": args.reason, "original_failures": failed,
        "base_observations_sha256": hashlib.sha256(base_bytes).hexdigest(),
        "retry_observations_sha256": hashlib.sha256(retry_bytes).hexdigest(),
        "retry_generator_hashes": retry.get("generator_hashes", {}),
        "retry_tests": sorted(retry_ids),
        "read_recovery": "Retried test file reads replaced by successful observations; failed-run diagnostic reads excluded. Other dependency categories remain unioned.",
        "excluded_failed_read_edges": removed_reads,
        "prefix_provenance": ({"sha256": hashlib.sha256(prefix_path.read_bytes()).hexdigest(),
                               "executed_tests": len(prefix["executed_prefix"]),
                               "mode": prefix["mode"], "complete": True,
                               "original_generator_identity": (
                                   "recorded" if base.get("generator_hashes") else
                                   "unavailable in legacy raw trace; no claim of identical observer source"),
                               "source_verification": source_verification,
                               "trace_seal_sha256": hashlib.sha256(seal_path.read_bytes()).hexdigest(),
                               "capture_embedded_hashes": seal["capture_embedded_hashes"],
                               "source_hashes": prefix["source_hashes"]} if prefix else None),
    })
    validate_observations(base)
    args.output.mkdir(parents=True, exist_ok=True)
    combined = coverage.CoverageData(basename=str(args.output / ".coverage"))
    for data in (base_data, retry_data):
        combined.update(data)
    combined.write()
    (args.output / "observations.json").write_text(json.dumps(base), encoding="utf-8")
    print(json.dumps({"recovered_tests": sorted(retry_ids), "original_failures": len(failed)}))


if __name__ == "__main__":
    main()
