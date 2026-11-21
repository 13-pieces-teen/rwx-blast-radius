"""Observe actual baseline comparison operands, never alternative operations.

Only exact immutable builtins are serializable. Unsupported operands, oversized
values and overflowing sites make that site opaque, so it cannot justify a proof.
"""
import ast
import importlib.abc
import importlib.machinery
import math
import operator
from pathlib import Path
import sys


HELPER = "__rwx_baseline_comparison_values__"
OPERATIONS = {"Eq": operator.eq, "NotEq": operator.ne, "Lt": operator.lt,
              "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge}


def encode(value, depth=0):
    kind = type(value)
    if value is None:
        return ("none",)
    if kind is bool:
        return ("bool", value)
    if kind is int and value.bit_length() <= 256:
        return ("int", str(value))
    if kind is float and math.isfinite(value):
        return ("float", value.hex())
    if kind is str and len(value) <= 256:
        return ("str", value)
    if kind is bytes and len(value) <= 256:
        return ("bytes", value.hex())
    if kind is tuple and depth < 3 and len(value) <= 12:
        return ("tuple", tuple(encode(item, depth + 1) for item in value))
    raise ValueError("opaque operand")


def decode(value):
    kind = value[0]
    if kind == "none":
        return None
    if kind == "bool":
        return value[1]
    if kind == "int":
        return int(value[1])
    if kind == "float":
        return float.fromhex(value[1])
    if kind == "str":
        return value[1]
    if kind == "bytes":
        return bytes.fromhex(value[1])
    if kind == "tuple":
        return tuple(decode(item) for item in value[1])
    raise ValueError("invalid operand tag")


def eligible(node):
    return (isinstance(node, ast.Compare) and len(node.ops) == 1
            and type(node.ops[0]).__name__ in OPERATIONS)


class ComparisonObserver(ast.NodeTransformer):
    def __init__(self, relative):
        self.relative = relative

    def visit_Compare(self, node):
        self.generic_visit(node)
        if not eligible(node):
            return node
        site = f"{self.relative}:{node.lineno}:{node.col_offset}"
        return ast.copy_location(ast.Call(func=ast.Name(id=HELPER, ctx=ast.Load()),
            args=[ast.Constant(site), ast.Constant(type(node.ops[0]).__name__),
                  node.left, node.comparators[0]], keywords=[]), node)


class ValueObserver:
    def __init__(self, limit=128):
        self.limit = limit
        self.sites = {}

    def compare(self, site, operation, left, right):
        record = self.sites.setdefault(site, {"operation": operation, "opaque": False,
                                              "observations": set()})
        # Perform the original operation exactly once, returning its actual
        # object (not bool(result)). Never invoke user conversions to log values.
        try:
            result = OPERATIONS[operation](left, right)
        except BaseException:
            record["opaque"] = True
            record["observations"].clear()
            raise
        if not record["opaque"]:
            try:
                if type(result) is not bool:
                    raise ValueError("non-boolean rich comparison")
                observation = (encode(left), encode(right), result)
                record["observations"].add(observation)
                if len(record["observations"]) > self.limit:
                    raise ValueError("site observation limit")
            except ValueError:
                record["opaque"] = True
                record["observations"].clear()
        return result

    def install(self, source):
        observer = self

        class Loader(importlib.machinery.SourceFileLoader):
            def get_code(self, fullname):
                path = Path(self.path)
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=self.path)
                transform = ComparisonObserver(path.relative_to(source).as_posix())
                return compile(ast.fix_missing_locations(transform.visit(tree)), self.path,
                               "exec", dont_inherit=True)

            def exec_module(self, module):
                module.__dict__[HELPER] = observer.compare
                super().exec_module(module)

        class Finder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
                    return None
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                if spec and spec.origin and spec.origin.endswith(".py"):
                    spec.loader = Loader(fullname, spec.origin)
                return spec

        sys.meta_path.insert(0, Finder())
