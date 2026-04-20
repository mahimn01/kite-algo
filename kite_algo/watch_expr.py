"""Safe AST-based expression evaluator for `watch --until`.

An agent typing `watch quote --until "last_price > 1300"` must not be able
to inject arbitrary Python. We parse the expression into a restricted AST
and evaluate it ourselves — no `eval()`, no `exec()`, no attribute access,
no calls, no subscripts.

Allowed:
- Comparisons: `<, <=, ==, !=, >=, >`
- Logical: `and, or, not`
- Arithmetic: `+, -, *, /, %`
- Names (bound to the current snapshot dict)
- Number / string / bool / None literals

Disallowed (AST-rejected at parse time):
- function calls
- attribute access (a.b)
- subscripts (a[b]) — agents use flat keys
- comprehensions
- lambdas
- assignments, import, etc.
"""

from __future__ import annotations

import ast
import operator
from typing import Any


# Operator table.
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}
_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}


class UnsafeExpression(ValueError):
    """Raised when a disallowed node type appears in the expression."""


def _eval_node(node: ast.AST, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, env)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        # Unknown names in the snapshot → None (never raise; makes the
        # expression tolerant to fields the broker sometimes omits).
        return None
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise UnsafeExpression(f"unary {type(node.op).__name__} not allowed")
        return op(_eval_node(node.operand, env))
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise UnsafeExpression(f"binary {type(node.op).__name__} not allowed")
        return op(_eval_node(node.left, env), _eval_node(node.right, env))
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(v, env) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
        raise UnsafeExpression(f"bool op {type(node.op).__name__} not allowed")
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, env)
        for op, comp in zip(node.ops, node.comparators):
            fn = _CMP_OPS.get(type(op))
            if fn is None:
                raise UnsafeExpression(f"comparator {type(op).__name__} not allowed")
            right = _eval_node(comp, env)
            # Treat None-comparison-with-number as False so snapshots that
            # don't yet have a value don't crash the watch loop.
            if left is None or right is None:
                if isinstance(op, (ast.Eq, ast.NotEq)):
                    # None == None is True; None == 0 is False.
                    result = fn(left, right)
                else:
                    return False
            else:
                result = fn(left, right)
            if not result:
                return False
            left = right
        return True
    # Any other node type is rejected.
    raise UnsafeExpression(f"{type(node).__name__} not allowed in watch expression")


def evaluate(expr: str, snapshot: dict[str, Any]) -> bool:
    """Evaluate `expr` against `snapshot`. Returns a bool (truthy coerced).

    Raises `UnsafeExpression` on disallowed syntax, `SyntaxError` on parse
    failure. Unknown names in the snapshot resolve to None.
    """
    if not expr or not expr.strip():
        raise ValueError("empty expression")
    tree = ast.parse(expr, mode="eval")
    return bool(_eval_node(tree, snapshot))
