"""Generate structural mutations from baseline source and coverage, without labels."""
import argparse
import ast
from collections import defaultdict
import difflib
import gzip
import json
from pathlib import Path
import random


def candidates(text):
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            first = next((n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))), None)
            if first is not None:
                indent = lines[first.lineno - 1][:len(lines[first.lineno - 1]) - len(lines[first.lineno - 1].lstrip())]
                yield "insert_raise", first.lineno, first.lineno - 1, first.lineno - 1, indent + "raise RuntimeError('structural probe')\n"
                yield "insert_return", first.lineno, first.lineno - 1, first.lineno - 1, indent + "return None\n"
            if node.decorator_list:
                decorator = node.decorator_list[0]
                yield "remove_decorator", node.lineno, decorator.lineno - 1, decorator.end_lineno, ""
            header = lines[node.lineno - 1]
            if ")" in header and "(" in header:
                before, after = header.split("(", 1)
                if after.lstrip().startswith(")"):
                    replacement = before + "(_rwx_required" + after
                else:
                    replacement = before + "(_rwx_required, " + after
                yield "change_signature", node.lineno, node.lineno - 1, node.lineno, replacement
        elif isinstance(node, (ast.Return, ast.Assign, ast.AugAssign, ast.Import, ast.ImportFrom)):
            parents = [f for f in functions if f.body[0].lineno <= node.lineno <= f.end_lineno]
            if parents:
                kind = "remove_import" if isinstance(node, (ast.Import, ast.ImportFrom)) else "remove_statement"
                indent = lines[node.lineno - 1][:len(lines[node.lineno - 1]) - len(lines[node.lineno - 1].lstrip())]
                yield kind, node.lineno, node.lineno - 1, node.end_lineno, indent + "pass\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--maps", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=902711)
    args = parser.parse_args()
    maps = json.loads(gzip.decompress(args.maps.read_bytes()))
    rng = random.Random(args.seed)
    options = defaultdict(list)
    for file in sorted((args.source / "sqlglot").rglob("*.py")):
        path = file.relative_to(args.source).as_posix()
        text = file.read_text(encoding="utf-8")
        for kind, line, start, end, replacement in candidates(text):
            covered = maps["lines"].get(path, {}).get(str(line), "0")
            if not int(covered, 16) or line in maps["collection_lines"].get(path, []):
                continue
            options[kind].append((path, line, text, start, end, replacement))
    rows, used = [], set()
    commits = args.output / "commits"
    commits.mkdir(parents=True, exist_ok=True)
    for kind in sorted(options):
        rng.shuffle(options[kind])
        count = 0
        for path, line, original, start, end, replacement in options[kind]:
            if path in used:
                continue
            lines = original.splitlines(keepends=True)
            modified = "".join(lines[:start]) + replacement + "".join(lines[end:])
            try:
                compile(modified, path, "exec")
            except (SyntaxError, ValueError):
                continue
            used.add(path)
            name = f"struct-{len(rows) + 1:03d}"
            patch = "".join(difflib.unified_diff(original.splitlines(True), modified.splitlines(True),
                                               fromfile="a/" + path, tofile="b/" + path))
            (commits / (name + ".patch")).write_text(patch, encoding="utf-8")
            rows.append({"id": name, "kind": kind, "path": path, "line": line})
            count += 1
            if count == 2:
                break
    (args.output / "manifest.json").write_text(json.dumps({"seed": args.seed, "mutations": rows}, indent=2) + "\n")
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
