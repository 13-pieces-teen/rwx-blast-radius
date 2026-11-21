"""Stress type-based reasoning without reading public patches or answers.

Each probe flips every eligible comparison in one semantic category at once.
The output is validation input only and never enters the submission image.
"""
from __future__ import annotations
import argparse
import ast
from collections import defaultdict
import difflib
import gzip
import io
import json
from pathlib import Path
import sys
import tokenize

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selector import identity_equivalent_types


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    profiles = json.loads(gzip.decompress(args.profiles.read_bytes()))
    types = profiles["types"]
    buckets = defaultdict(lambda: defaultdict(list))
    sources = {}
    replacements = {ast.Is: "==", ast.IsNot: "!=", ast.Eq: "is", ast.NotEq: "is not"}
    for path in sorted((args.source / "sqlglot").rglob("*.py")):
        relative = path.relative_to(args.source).as_posix()
        text = path.read_text(encoding="utf-8")
        sources[relative] = text
        lines = text.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))

        def offset(row, col, ast_column=False):
            if ast_column:
                col = len(lines[row - 1].encode()[:col].decode())
            return offsets[row - 1] + col

        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1 or type(node.ops[0]) not in replacements:
                continue
            site = f"{relative}:{node.lineno}:{node.col_offset}"
            observations = profiles["comparisons"].get(site)
            negative = isinstance(node.ops[0], (ast.IsNot, ast.NotEq))
            if not observations or not all(identity_equivalent_types(types[a], types[b], am, bm, negative)
                                           for a, b, am, bm in observations):
                continue
            category = ("none" if any(types[a]["none"] or types[b]["none"] for a, b, _, _ in observations)
                        else "enum" if any(am and bm for _, _, am, bm in observations) else "object")
            begin = offset(node.left.end_lineno, node.left.end_col_offset, True)
            end = offset(node.comparators[0].lineno, node.comparators[0].col_offset, True)
            ops = [t for t in tokens if t.string in ("is", "not", "==", "!=")
                   and begin <= offset(*t.start) < end]
            if not ops:
                raise ValueError(site)
            start, stop = offset(*ops[0].start), offset(*ops[-1].end)
            buckets[category][relative].append((start, stop, replacements[type(node.ops[0])], site))
    commits = args.output / "commits"
    commits.mkdir(parents=True, exist_ok=True)
    rows = []
    for category, files in sorted(buckets.items()):
        patches, sites = [], []
        for relative, changes in sorted(files.items()):
            original = sources[relative]
            modified = original
            for start, stop, replacement, site in sorted(changes, reverse=True):
                modified = modified[:start] + replacement + modified[stop:]
                sites.append(site)
            compile(modified, relative, "exec")
            patches.extend(difflib.unified_diff(original.splitlines(True), modified.splitlines(True),
                                               fromfile="a/" + relative, tofile="b/" + relative))
        name = "identity-" + category
        (commits / (name + ".patch")).write_text("".join(patches), encoding="utf-8")
        rows.append({"id": name, "files": len(files), "sites": sorted(sites)})
    (args.output / "manifest.json").write_text(json.dumps({"mutations": rows}, indent=2) + "\n")
    print(json.dumps([{**r, "sites": len(r["sites"])} for r in rows]))


if __name__ == "__main__":
    main()
