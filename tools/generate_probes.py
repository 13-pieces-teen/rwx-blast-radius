"""Create deterministic, previously unseen source mutations for offline validation.

Does not read sample patches or answers. Probe files remain under evaluation/,
which is excluded from Git and the submission image.
"""
from __future__ import annotations
import argparse
import ast
from collections import defaultdict
import difflib
import json
from pathlib import Path
import random
import re


def candidates(text):
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def offset(row, byte_col):
        return offsets[row - 1] + len(lines[row - 1].encode()[:byte_col].decode())

    operations = {ast.Eq: ("==", "!="), ast.NotEq: ("!=", "=="),
                  ast.Lt: ("<", "<="), ast.LtE: ("<=", "<"),
                  ast.Gt: (">", ">="), ast.GtE: (">=", ">"),
                  ast.Is: ("is", "is not"), ast.IsNot: ("is not", "is"),
                  ast.In: ("in", "not in"), ast.NotIn: ("not in", "in")}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (bool, int)):
            start, end = offset(node.lineno, node.col_offset), offset(node.end_lineno, node.end_col_offset)
            value = not node.value if isinstance(node.value, bool) else node.value + 1
            yield start, end, repr(value), node.lineno, "constant"
        elif isinstance(node, (ast.Compare, ast.BoolOp)):
            if isinstance(node, ast.Compare):
                before, after = operations[type(node.ops[0])]
                left, right = node.left, node.comparators[0]
            else:
                before, after = ("and", "or") if isinstance(node.op, ast.And) else ("or", "and")
                left, right = node.values[:2]
            start = offset(left.end_lineno, left.end_col_offset)
            end = offset(right.lineno, right.col_offset)
            match = re.search(re.escape(before), text[start:end])
            if match:
                yield start + match.start(), start + match.end(), after, left.end_lineno, "operator"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--count", type=int, default=24)
    p.add_argument("--seed", type=int, default=20261121)
    args = p.parse_args()
    rng = random.Random(args.seed)
    buckets = defaultdict(list)
    for file in sorted((args.source / "sqlglot").rglob("*.py")):
        relative = file.relative_to(args.source).as_posix()
        text = file.read_text(encoding="utf-8")
        options = list(candidates(text))
        if not options:
            continue
        group = relative.split("/")[1] if relative.count("/") > 1 else "core"
        if group in ("parsers", "generators", "dialects", "typing"):
            group = "dialect"
        elif group not in ("core", "optimizer", "expressions"):
            group = "other"
        buckets[group].append((relative, text, options))
    for entries in buckets.values():
        rng.shuffle(entries)
    changes = []
    groups = list(buckets)
    while len(changes) < args.count and any(buckets.values()):
        for group in groups:
            if not buckets[group] or len(changes) >= args.count:
                continue
            relative, text, options = buckets[group].pop()
            rng.shuffle(options)
            for start, end, replacement, line, kind in options:
                modified = text[:start] + replacement + text[end:]
                try:
                    compile(modified, relative, "exec")
                except (SyntaxError, ValueError):
                    continue
                patch = "".join(difflib.unified_diff(text.splitlines(True), modified.splitlines(True),
                                                    fromfile="a/" + relative, tofile="b/" + relative))
                changes.append({"id": f"probe-{len(changes)+1:03d}", "path": relative,
                                "line": line, "kind": kind, "old": text[start:end],
                                "new": replacement, "patch": patch})
                break
    commits = args.output / "commits"
    commits.mkdir(parents=True, exist_ok=True)
    for change in changes:
        (commits / (change["id"] + ".patch")).write_text(change.pop("patch"), encoding="utf-8")
    (args.output / "manifest.json").write_text(json.dumps({"seed": args.seed, "mutations": changes}, indent=2) + "\n")
    print(json.dumps(changes, indent=2))


if __name__ == "__main__":
    main()
