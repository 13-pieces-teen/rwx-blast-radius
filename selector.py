"""Choose tests from pinned-source coverage and dependencies; minimize CI cost.

Persistent maps come from the unchanged SQLGlot tree, never failure labels.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import types


@dataclass
class FileChange:
    old: str
    new: str
    hunks: list = field(default_factory=list)


def patch_path(value: str) -> str:
    value = value.split("\t", 1)[0].strip()
    if value.startswith('"'):
        value = shlex.split(value)[0]
    if value.startswith(("a/", "b/")):
        value = value[2:]
    if value != "/dev/null":
        p = PurePosixPath(value)
        if p.is_absolute() or ".." in p.parts or "\\" in value:
            raise ValueError("unsafe diff path")
    return value


def parse_patch(text: str) -> list[FileChange]:
    changes = []
    lines = text.splitlines(keepends=True)
    i = 0
    current = None
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise ValueError("missing new path")
            current = FileChange(patch_path(line[4:]), patch_path(lines[i + 1][4:]))
            changes.append(current)
            i += 2
            continue
        if line.startswith("@@"):
            header = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if current is None or header is None:
                raise ValueError("invalid hunk")
            start, count, new_start, new_count = (int(x) if x is not None else 1 for x in header.groups())
            body = []
            i += 1
            old_seen = new_seen = 0
            while i < len(lines) and (old_seen < count or new_seen < new_count):
                part = lines[i]
                if part.startswith("\\ No newline"):
                    if body:
                        body[-1] = body[-1].rstrip("\r\n")
                    i += 1
                    continue
                if not part or part[0] not in " +-":
                    raise ValueError("invalid hunk line")
                body.append(part)
                old_seen += part[0] in " -"
                new_seen += part[0] in " +"
                i += 1
            if i < len(lines) and lines[i].startswith("\\ No newline"):
                body[-1] = body[-1].rstrip("\r\n")
                i += 1
            if old_seen != count or new_seen != new_count:
                raise ValueError("hunk size mismatch")
            current.hunks.append((start, count, new_start, new_count, body))
            continue
        i += 1
    if not changes:
        raise ValueError("no readable file changes")
    return changes


def apply_change(source: str, change: FileChange):
    original = source.splitlines(keepends=True)
    result = []
    cursor = 0
    touched = set()
    insertions = set()
    for start, count, _, _, body in change.hunks:
        position = start - 1 if count else start
        if position < cursor or position > len(original):
            raise ValueError("overlapping or out-of-range hunk")
        result.extend(original[cursor:position])
        cursor = position
        group_deleted = False
        for part in body:
            prefix, content = part[0], part[1:]
            if prefix in " -":
                if cursor >= len(original) or original[cursor].rstrip("\r\n") != content.rstrip("\r\n"):
                    raise ValueError("patch does not match pinned source")
                if prefix == "-":
                    touched.add(cursor + 1)
                    group_deleted = True
                else:
                    result.append(original[cursor])
                    group_deleted = False
                cursor += 1
            elif prefix == "+":
                result.append(content)
                if not group_deleted:
                    insertions.add(cursor + 1)
    result.extend(original[cursor:])
    return "".join(result), touched, insertions


def schedule(selected: list[dict]) -> list[list[int]]:
    if not selected:
        return []
    jobs = sorted(((int(t["seconds"] * 1000 + 0.5), t["number"]) for t in selected), reverse=True)
    total = sum(d for d, _ in jobs)
    longest = jobs[0][0]
    best_cost = total + 2000
    best = [[number for _, number in jobs]]
    for count in range(2, min(8, len(jobs)) + 1):
        if max(longest, (total + count - 1) // count) + count * 2000 >= best_cost:
            continue
        loads = [0] * count
        groups = [[] for _ in range(count)]
        for duration, number in jobs:
            target = min(range(count), key=loads.__getitem__)
            loads[target] += duration
            groups[target].append(number)
        cost = max(loads) + 2000 * count
        if cost < best_cost:
            best_cost, best = cost, groups
    return [group for group in best if group]


def class_fields(tree):
    """Find ordinary declarations, retaining their class scope and syntax."""
    found = {}

    def visit(body, scope=()):
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, (*scope, node.name))
            elif scope and isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if len(targets) == 1 and isinstance(targets[0], ast.Name):
                    annotation = ast.dump(node.annotation) if isinstance(node, ast.AnnAssign) else None
                    signature = (type(node).__name__, ast.dump(targets[0]), annotation,
                                 getattr(node, "type_comment", None), getattr(node, "simple", None))
                    found[(scope, targets[0].id)] = (node, signature)
    visit(tree.body)
    return found


def identity_edits(before, after):
    """Accept only same-polarity identity/equality replacements in an AST."""
    edits = []
    pairs = {(ast.Is, ast.Eq), (ast.Eq, ast.Is),
             (ast.IsNot, ast.NotEq), (ast.NotEq, ast.IsNot)}

    def same(left, right):
        if type(left) is not type(right):
            return False
        if isinstance(left, ast.AST):
            skip_ops = (isinstance(left, ast.Compare) and len(left.ops) == len(right.ops) == 1
                        and (type(left.ops[0]), type(right.ops[0])) in pairs)
            if skip_ops:
                edits.append((left.lineno, left.col_offset,
                              isinstance(left.ops[0], (ast.IsNot, ast.NotEq))))
            return all(same(getattr(left, key), getattr(right, key))
                       for key in left._fields if not (skip_ops and key == "ops"))
        if isinstance(left, list):
            return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
        return left == right

    return edits if same(before, after) else []


def identity_equivalent_types(left, right, left_member, right_member, negative):
    """Prove the relation using Python's built-in slots and observed enum metadata.

    Custom rich comparisons are deliberately outside this proof. No substitute
    operator was evaluated while collecting these facts from the baseline.
    """
    builtins = {"object", "none", "str", "int", "float", "bytes", "list", "tuple", "dict", "set", "frozenset"}
    # Some Python versions give NoneType its own immutable comparison slots.
    # The profiler records exact NoneType identity separately from slot tags.
    eq = ["none" if t["none"] else t["eq"] for t in (left, right)]
    ne = ["none" if t["none"] else t["ne"] for t in (left, right)]
    if any(slot not in builtins for slot in eq):
        return False
    if negative and any(n not in ("object", e) for e, n in zip(eq, ne)):
        return False
    if left["eq"] == right["eq"] == "object":
        return True
    if left["none"] or right["none"]:
        return True
    if (left["type_identity"] == right["type_identity"] and left_member and right_member
            and left["eq"] == right["eq"] and left["eq"] in ("str", "int")
            and left["enum_values"] is not None and left["enum_values"] == right["enum_values"]):
        values = left["enum_values"]
        return len(values) == len(set(values))
    return False


class Selector:
    def __init__(self, source: Path, maps: Path, tests: list[dict]):
        self.source = source
        self.tests = tests
        self.map = json.loads(gzip.decompress(maps.read_bytes()))
        positions = {test: i for i, test in enumerate(self.map["ids"])}
        self.numbers = {positions[t["id"]]: t["number"] for t in tests if t["id"] in positions}
        self.unknown = {t["number"] for t in tests if t["id"] not in positions}
        self.all_bits = sum(1 << n for n in self.numbers)
        self.cache = {}
        self.line_cache = {}
        self.collection_attributes = set(self.map.get("collection_attributes", []))
        self.literal_readers = {}
        self.reflections = {}
        self.code_cache = {}
        reflection_path = maps.parent / "reflection-fields.json.gz"
        if reflection_path.exists():
            reflection = json.loads(gzip.decompress(reflection_path.read_bytes()))
            if (reflection.get("complete") and not reflection.get("failures")
                    and reflection.get("source_commit") == self.map.get("source_commit")
                    and reflection.get("ids") == self.map["ids"]):
                for function in reflection["functions"]:
                    self.reflections.setdefault(function["path"], []).append(function)
        self.profiles = None
        profile_path = maps.parent / "type-profiles.json.gz"
        if profile_path.exists():
            profiles = json.loads(gzip.decompress(profile_path.read_bytes()))
            if (profiles.get("complete") and not profiles.get("failures")
                    and profiles.get("source_commit") == self.map.get("source_commit")
                    and profiles.get("ids") == self.map["ids"]):
                self.profiles = profiles

    def bits(self, category, key):
        return int(self.map[category].get(key, "0"), 16)

    def lines(self, path, first, last):
        if path not in self.line_cache:
            self.line_cache[path] = {int(k): int(v, 16) for k, v in self.map["lines"].get(path, {}).items()}
        value = 0
        by_line = self.line_cache[path]
        for line in range(first, last + 1):
            value |= by_line.get(line, 0)
        return value

    def load_source(self, path):
        if path not in self.cache:
            text = (self.source / path).read_text(encoding="utf-8")
            expected = self.map["source_hashes"].get(path)
            if expected is not None and hashlib.sha256(text.encode()).hexdigest() != expected:
                raise ValueError("source mismatch")
            tree = ast.parse(text, filename=path)
            nodes = [n for n in ast.walk(tree) if hasattr(n, "lineno")]
            self.cache[path] = text, tree, nodes
        return self.cache[path]

    def module(self, path):
        value = self.bits("modules", path) | self.bits("reads", path)
        for index in self.numbers:
            if self.map["ids"][index].split("::", 1)[0] == path:
                value |= 1 << index
        return value or self.all_bits

    def equivalent_operators(self, path, tree, new_tree):
        if self.profiles is None:
            return False
        edits = identity_edits(tree, new_tree)
        if not edits:
            return False
        types = self.profiles["types"]
        for line, column, negative in edits:
            observations = self.profiles["comparisons"].get(f"{path}:{line}:{column}")
            if not observations:
                return False
            if not all(identity_equivalent_types(types[a], types[b], am, bm, negative)
                       for a, b, am, bm in observations):
                return False
        return True

    @staticmethod
    def code_objects(source, filename):
        found = {}

        def visit(code):
            found.setdefault(code.co_qualname, []).append(code)
            for value in code.co_consts:
                if isinstance(value, types.CodeType):
                    visit(value)

        visit(compile(source, filename, "exec", dont_inherit=True))
        return found

    def reflection_impact(self, path, original, modified):
        records = self.reflections.get(path)
        if not records:
            return 0
        filename = str(self.source / path)
        if path not in self.code_cache:
            self.code_cache[path] = self.code_objects(original, filename)
        old_codes = self.code_cache[path]
        new_codes = self.code_objects(modified, filename)
        selected = 0
        comparable = {"co_argcount", "co_cellvars", "co_code", "co_consts", "co_exceptiontable",
                      "co_filename", "co_firstlineno", "co_flags", "co_freevars", "co_kwonlyargcount",
                      "co_linetable", "co_lnotab", "co_name", "co_names", "co_nlocals",
                      "co_posonlyargcount", "co_qualname", "co_stacksize", "co_varnames"}
        for record in records:
            before = old_codes.get(record["name"], [])
            after = new_codes.get(record["name"], [])
            indexes = [i for i, code in enumerate(before) if code.co_firstlineno == record["line"]]
            matched = bool(indexes) and len(before) == len(after)
            for field in set(record["fields"]) | set(record["collection"]):
                changed = (not matched or field not in comparable or
                           any(getattr(before[i], field, None) != getattr(after[i], field, None) for i in indexes))
                if changed:
                    if field in record["collection"]:
                        return self.all_bits
                    selected |= int(record["fields"].get(field, "0"), 16)
        return selected

    def line_impact(self, path, line, nodes, insertion=False):
        enclosing = [n for n in nodes if n.lineno <= line <= n.end_lineno]
        functions = [n for n in enclosing if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        functions.sort(key=lambda n: n.end_lineno - n.lineno)
        function = functions[0] if functions else None
        if function is not None and line >= function.body[0].lineno:
            collection = set(self.map.get("collection_lines", {}).get(path, []))
            if insertion:
                if any(function.body[0].lineno <= n <= function.end_lineno for n in collection):
                    return self.all_bits
                return self.lines(path, function.lineno, function.end_lineno)
            statements = [n for n in enclosing if isinstance(n, ast.stmt) and
                          not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            if statements:
                statement = min(statements, key=lambda n: (n.end_lineno - n.lineno, -n.lineno))
                if isinstance(statement, (ast.If, ast.While)) and line <= statement.test.end_lineno:
                    target = statement.test
                elif isinstance(statement, (ast.For, ast.AsyncFor)) and line <= statement.iter.end_lineno:
                    target = statement.iter
                else:
                    target = statement
                if any(target.lineno <= n <= target.end_lineno for n in collection):
                    # A helper executed during collection can materialize state
                    # consumed elsewhere without any later call to this helper.
                    return self.all_bits
                return self.lines(path, target.lineno, target.end_lineno)
            return self.lines(path, function.lineno, function.end_lineno)
        if function is not None:
            return self.lines(path, function.lineno, function.end_lineno) | self.module(path)
        lambdas = [n for n in enclosing if isinstance(n, ast.Lambda) and line >= n.body.lineno]
        if lambdas and not insertion:
            target = min(lambdas, key=lambda n: n.end_lineno - n.lineno)
            value = self.lines(path, target.body.lineno, target.body.end_lineno)
            if value:
                return value
        classes = [n for n in enclosing if isinstance(n, ast.ClassDef)]
        classes.sort(key=lambda n: n.end_lineno - n.lineno)
        if classes and not insertion:
            readers = self.literal_readers.get(path, {})
            for node in enclosing:
                if id(node) in readers:
                    return readers[id(node)] | self.lines(path, line, line)
        for cls in classes:
            value = self.bits("symbols", path + ":" + cls.name)
            if value:
                return value | self.lines(path, line, line)
        return self.module(path)

    def select_change(self, change):
        path = change.old
        if path == "/dev/null" or change.new == "/dev/null" or path != change.new:
            return self.all_bits
        if not path.endswith(".py"):
            return self.bits("reads", path) or self.all_bits
        source, tree, nodes = self.load_source(path)
        modified, touched, insertions = apply_change(source, change)
        try:
            new_tree = ast.parse(modified, filename=path)
            compile(modified, path, "exec")
        except (SyntaxError, ValueError):
            return self.all_bits
        reflected = self.reflection_impact(path, source, modified)
        if ast.dump(tree, include_attributes=False) == ast.dump(new_tree, include_attributes=False):
            return self.bits("reads", path) | reflected
        if self.equivalent_operators(path, tree, new_tree):
            return self.bits("reads", path) | reflected
        self.literal_readers[path] = {}
        old_fields, new_fields = class_fields(tree), class_fields(new_tree)
        for key in old_fields.keys() & new_fields.keys():
            old_node, old_signature = old_fields[key]
            new_node, new_signature = new_fields[key]
            scope, name = key
            attribute = path + ":" + scope[-1] + ":" + name
            readers = self.map.get("attributes", {}).get(attribute)
            if old_signature != new_signature or readers is None or attribute in self.collection_attributes:
                continue
            try:
                # Calls, names and other expressions could introduce side effects.
                ast.literal_eval(old_node.value)
                ast.literal_eval(new_node.value)
            except (ValueError, TypeError, SyntaxError):
                continue
            self.literal_readers[path][id(old_node)] = int(readers, 16)
        self.overrides[path] = modified
        for line in touched | insertions:
            inside_body = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                              and n.body[0].lineno <= line <= n.end_lineno for n in nodes)
            inside_lambda = any(isinstance(n, ast.Lambda) and n.body.lineno <= line <= n.end_lineno
                                for n in nodes)
            if not inside_body and not inside_lambda:
                self.import_needed = True
        value = self.bits("reads", path) | reflected
        for line in touched:
            value |= self.line_impact(path, line, nodes)
        for line in insertions:
            value |= self.line_impact(path, line, nodes, insertion=True)
        return value

    def select(self, patch):
        self.overrides = {}
        self.import_needed = False
        try:
            selected = 0
            for change in parse_patch(patch):
                selected |= self.select_change(change)
            if self.import_needed and selected != self.all_bits:
                imports = [p for p, lines in self.map.get("collection_lines", {}).items()
                           if p.startswith("sqlglot/") and lines and not p.endswith("/__main__.py")]
                request = json.dumps({"source": str(self.source), "overrides": self.overrides,
                                      "imports": imports}).encode()
                checker = Path(__file__).parent / "tools/import_check.py"
                result = subprocess.run([sys.executable, "-B", str(checker)], input=request,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=8)
                if result.returncode:
                    print("import validation failed; selecting all tests", file=sys.stderr)
                    selected = self.all_bits
        except (OSError, ValueError, SyntaxError, RecursionError, subprocess.TimeoutExpired) as error:
            print(f"conservative fallback: {type(error).__name__}: {error}", file=sys.stderr)
            selected = self.all_bits
        numbers = set(self.unknown)
        while selected:
            low = selected & -selected
            index = low.bit_length() - 1
            if index in self.numbers:
                numbers.add(self.numbers[index])
            selected ^= low
        return [t for t in self.tests if t["number"] in numbers]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    parser.add_argument("--maps", type=Path, default=Path(__file__).parent / "artifacts/baseline-map.json.gz")
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_text())["tests"]
    selector = Selector(args.source, args.maps, tests)
    for path in sorted((args.data / "commits").glob("*.patch")):
        selected = selector.select(path.read_text(encoding="utf-8"))
        workers = schedule(selected)
        print(json.dumps({"commit": path.stem, "workers": workers}, separators=(",", ":")))
        print(f"{path.stem}: {len(selected)} tests, {len(workers)} workers", file=sys.stderr)


if __name__ == "__main__":
    main()
