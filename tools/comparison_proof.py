"""Prove globally unchanged builtin comparison results from baseline operands.

This never narrows individual test contexts: every observed input in every
required baseline run must produce the same boolean. Unknown cases reject it.
"""
import ast

from tools.comparison_values import OPERATIONS, decode, encode, eligible


def pure_literal(node):
    if any(isinstance(child, ast.Call) for child in ast.walk(node)):
        raise ValueError("literal contains call")
    value = ast.literal_eval(node)
    encode(value)
    return value


def comparison_edits(before, after):
    edits = []

    def match(left, right):
        if type(left) is not type(right):
            return False
        if isinstance(left, ast.AST):
            if eligible(left) and eligible(right):
                if ast.dump(left) == ast.dump(right):
                    return True
                replacements = []
                for a, b in zip((left.left, left.comparators[0]), (right.left, right.comparators[0])):
                    if ast.dump(a) == ast.dump(b):
                        replacements.append(None)
                    else:
                        try:
                            # Changing only a direct constant operand preserves
                            # all calls and side effects in the other operand.
                            replacements.append((pure_literal(a), pure_literal(b)))
                        except (ValueError, TypeError, SyntaxError):
                            return False
                edits.append((left.lineno, left.col_offset, type(left.ops[0]).__name__,
                              type(right.ops[0]).__name__, replacements))
                return True
            return all(match(getattr(left, field, None), getattr(right, field, None)) for field in left._fields)
        if isinstance(left, list):
            return len(left) == len(right) and all(match(a, b) for a, b in zip(left, right))
        return left == right

    return edits if match(before, after) and edits else []


def equivalent_comparisons(path, before, after, profile):
    edits = comparison_edits(before, after)
    if not edits:
        return False
    for line, column, old_op, new_op, replacements in edits:
        record = profile["sites"].get(f"{path}:{line}:{column}")
        if (not record or record.get("opaque") or record.get("operation") != old_op
                or not record.get("observations")):
            return False
        for encoded_left, encoded_right, expected in record["observations"]:
            try:
                values = [decode(encoded_left), decode(encoded_right)]
                if type(expected) is not bool or OPERATIONS[old_op](*values) is not expected:
                    return False
                new_values = values[:]
                for i, replacement in enumerate(replacements):
                    if replacement is not None:
                        old_literal, new_literal = replacement
                        # Type and representation matter: bool/int and signed
                        # zero can compare equal while encoding distinct values.
                        if encode(values[i]) != encode(old_literal):
                            return False
                        new_values[i] = new_literal
                if OPERATIONS[new_op](*new_values) is not expected:
                    return False
            except (ValueError, TypeError, OverflowError):
                return False
    return True
