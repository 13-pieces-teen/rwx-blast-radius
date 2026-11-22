"""Dependencies introduced by a process executor shared across test methods.

The profile is measured on the unchanged source. Only worker initialization and
unpickling can poison an otherwise healthy pool; an ordinary callable exception
belongs to its Future. Consumers are discovered independently of failure labels.
"""
from __future__ import annotations

import ast
import gzip
import hashlib
import json
from pathlib import Path


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value)
        return parent + "." + node.attr if parent else ""
    return ""


def _source_models(sources):
    models = {}
    for path, text in sorted(sources.items()):
        if not path.startswith("tests/") or not path.endswith(".py"):
            continue
        tree = ast.parse(text, filename=path)
        module = path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
        aliases = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                parent = node.module or ""
                if node.level:
                    prefix = package.split(".")[:len(package.split(".")) - node.level + 1]
                    parent = ".".join([*prefix, *([parent] if parent else [])])
                for name in node.names:
                    aliases[name.asname or name.name] = parent + "." + name.name
            elif isinstance(node, ast.Import):
                for name in node.names:
                    if name.asname:
                        aliases[name.asname] = name.name
                    else:
                        aliases[name.name.split(".")[0]] = name.name.split(".")[0]
        models[path] = {"tree": tree, "module": module, "aliases": aliases,
                        "classes": {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}}
    return models


def _resolve_name(node, model):
    raw = _dotted(node)
    head, _, tail = raw.partition(".")
    return model["aliases"].get(head, head) + ("." + tail if tail else "")


def discover_local_factories(sources):
    """Only an explicit context-manager lifetime is certified as local.

    Unknown factories are rejected by full-suite dynamic discovery, rather than
    assuming that an executor allocated in a helper is confined to one test.
    """
    factories = []
    for path, model in _source_models(sources).items():
        for node in ast.walk(model["tree"]):
            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    call = item.context_expr
                    if (isinstance(call, ast.Call)
                            and _resolve_name(call.func, model) == "concurrent.futures.ProcessPoolExecutor"):
                        factories.append({"path": path, "factory_line": call.lineno,
                                          "lifetime": "explicit-context-manager"})
    return factories


def discover_shared_pools(sources, ids):
    """Discover class fixtures, including helpers/fixtures inherited across files."""
    models = _source_models(sources)
    class_locations = {model["module"] + "." + name: (path, name)
                       for path, model in models.items() for name in model["classes"]}

    def methods(path, name, active=()):
        key = (path, name)
        if key in active or name not in models[path]["classes"]:
            return {}
        model = models[path]
        cls = model["classes"][name]
        value = {}
        for base in reversed(cls.bases):
            dotted = _resolve_name(base, model)
            location = class_locations.get(dotted) or class_locations.get(model["module"] + "." + dotted)
            if location:
                value.update(methods(*location, (*active, key)))
        value.update({n.name: (path, n) for n in cls.body
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))})
        return value

    result = []
    for path, model in sorted(models.items()):
        for name in sorted(model["classes"]):
            prefix = f"{path}::{name}::"
            class_ids = sorted(i for i in ids if i.startswith(prefix))
            if not class_ids:
                continue
            found = methods(path, name)
            setup = found.get("setUpClass")
            if setup is None:
                continue
            setup_path, setup_node = setup
            for assign in ast.walk(setup_node):
                targets = assign.targets if isinstance(assign, ast.Assign) else (
                    [assign.target] if isinstance(assign, ast.AnnAssign) else [])
                call = getattr(assign, "value", None)
                if (not isinstance(call, ast.Call) or
                        _resolve_name(call.func, models[setup_path]) != "concurrent.futures.ProcessPoolExecutor"):
                    continue
                for target in targets:
                    if not (isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id in {"cls", "self"}):
                        continue
                    attribute = target.attr
                    users, callers = set(), {}
                    for method_name, (_, method) in found.items():
                        callers[method_name] = set()
                        for node in ast.walk(method):
                            if not isinstance(node, ast.Call):
                                continue
                            dotted = _dotted(node.func)
                            if dotted in {f"self.{attribute}.submit", f"cls.{attribute}.submit",
                                          f"self.{attribute}.map", f"cls.{attribute}.map"}:
                                users.add(method_name)
                            if dotted.startswith(("self.", "cls.")) and dotted.count(".") == 1:
                                callers[method_name].add(dotted.split(".")[1])
                    while True:
                        more = {m for m, called in callers.items() if called & users}
                        if more <= users:
                            break
                        users |= more
                    consumers = sorted(i for i in class_ids if i.rsplit("::", 1)[-1] in users)
                    result.append({"key": f"{path}::{name}::{attribute}", "path": path,
                                   "class": name, "attribute": attribute, "factory_path": setup_path,
                                   "factory_line": call.lineno, "lifetime": "class",
                                   "class_test_ids": class_ids, "static_consumers": consumers})
    return result


class SharedExecutorDependencies:
    """Additive pool closure, applied after all selection refinements.

    Missing/stale/malformed profiles raise instead of silently disabling a
    correctness guard. The caller must use its conservative selection fallback.
    """
    def __init__(self, selector, path=None):
        self.selector = selector
        path = Path(path) if path is not None else Path(__file__).parent / "artifacts/shared-executor-profile.json.gz"
        self.profile = json.loads(gzip.decompress(path.read_bytes()))
        p, base = self.profile, selector.map
        if not (p.get("format_version") == 1 and p.get("complete") is True
                and p.get("failures") == [] and p.get("source_commit") == base.get("source_commit")
                and p.get("source_hashes") == base.get("source_hashes") and p.get("ids") == base.get("ids")
                and p.get("phases") == ["initializer", "task_unpickle", "result_unpickle"]
                and p.get("full_suite_factory_audit") is True and p.get("unknown_factories") == []
                and p.get("fixture_source_hashes") and p.get("lifecycle_complete") is True and p.get("pools")):
            raise ValueError("missing or incompatible shared-executor profile")
        for filename, expected in p.get("fixture_source_hashes", {}).items():
            actual = hashlib.sha256((selector.source / filename).read_text(encoding="utf8").encode()).hexdigest()
            if actual != expected:
                raise ValueError("shared executor test fixture source mismatch")
        positions = {name: i for i, name in enumerate(base["ids"])}
        self.pools = []
        for pool in p["pools"]:
            if pool["lifetime"] != "class" or not pool["consumers"] or not pool["observed_submissions"]:
                raise ValueError("unobserved shared executor")
            if not set(pool["static_consumers"]) <= set(pool["consumers"]):
                raise ValueError("missing static pool consumers")
            bits = 0
            for name in pool["consumers"]:
                bits |= 1 << positions[name]
            spans = {}
            for fn in pool["poison_functions"]:
                if fn["first"] < 1 or fn["last"] < fn["first"]:
                    raise ValueError("invalid poison dependency")
                spans.setdefault(fn["path"], []).append((fn["first"], fn["last"]))
            lines = {path: set(values) for path, values in pool["poison_lines"].items()}
            self.pools.append((bits, spans, lines))

    def impact(self, patch):
        # Lazy import avoids a selector/module import cycle at integration time.
        from selector import apply_change, parse_patch
        result = 0
        for change in parse_patch(patch):
            path = change.old if change.old != "/dev/null" else change.new
            interested = [(bits, spans.get(path, []), lines.get(path, set()))
                          for bits, spans, lines in self.pools if path in spans or path in lines]
            if not interested:
                continue
            if change.old == "/dev/null" or change.new == "/dev/null":
                for bits, _, _ in interested:
                    result |= bits
                continue
            source, tree, _ = self.selector.load_source(path)
            new_source, touched, insertions = apply_change(source, change)
            ast.parse(new_source, filename=path)
            function_bodies = [(n.body[0].lineno, n.end_lineno)
                               for n in ast.walk(tree)
                               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.body]
            for bits, spans, lines in interested:
                edits = touched | insertions
                if any(line in lines or any(a <= line <= b + (line in insertions) for a, b in spans)
                       for line in edits):
                    result |= bits
                # Module/class declarations can change globals or class state
                # consumed by deserializers without executing the declaration
                # again. Be broad only in a transport-relevant source module.
                elif any(not any(a <= line <= b for a, b in function_bodies) for line in edits):
                    result |= bits
        return result

    def expand(self, patch, selected):
        bits = self.impact(patch)
        numbers = {test["number"] for test in selected}
        for index, number in self.selector.numbers.items():
            if bits & (1 << index):
                numbers.add(number)
        return [test for test in self.selector.tests if test["number"] in numbers]
