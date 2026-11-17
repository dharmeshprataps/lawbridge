"""Compile a deliberately small expression language without executing it."""

import ast

from .errors import ContractError

ROOTS = {"input", "before", "after", "result"}
BINOPS = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul"}
COMPARISONS = {
    ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le",
    ast.Gt: "gt", ast.GtE: "ge",
}


def _binary(kind, left, right):
    return {"kind": kind, "left": left, "right": right}


def compile_expression(source: str) -> dict:
    if not isinstance(source, str) or not source.strip():
        raise ContractError("law expressions must be nonempty strings")
    if len(source) > 4096:
        raise ContractError("law expression exceeds 4096 characters")
    try:
        tree = ast.parse(source, mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ContractError(f"invalid law expression {source!r}: {exc}") from exc
    if sum(1 for _ in ast.walk(tree)) > 512:
        raise ContractError("law expression is too complex")

    def visit(node, depth=0):
        if depth > 64:
            raise ContractError("law expression nesting exceeds 64")
        descend = lambda child: visit(child, depth + 1)
        if isinstance(node, ast.Constant):
            value = node.value
            if type(value) is bool:
                return {"kind": "bool", "value": value}
            if type(value) is int:
                return {"kind": "int", "value": value}
            if type(value) is str:
                return {"kind": "string", "value": value}
            raise ContractError("only integer, Boolean, and string literals are supported")
        if isinstance(node, (ast.Name, ast.Attribute)):
            parts = []
            cursor = node
            while isinstance(cursor, ast.Attribute):
                if cursor.attr.startswith("_"):
                    raise ContractError("private or special field names are unsupported")
                parts.append(cursor.attr)
                cursor = cursor.value
            if not isinstance(cursor, ast.Name) or cursor.id not in ROOTS:
                raise ContractError("paths must start with input, before, after, or result")
            return {"kind": "path", "value": [cursor.id, *reversed(parts)]}
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                return {"kind": "not", "arg": descend(node.operand)}
            if isinstance(node.op, ast.USub):
                return {"kind": "neg", "arg": descend(node.operand)}
        if isinstance(node, ast.BinOp) and type(node.op) in BINOPS:
            return _binary(BINOPS[type(node.op)], descend(node.left), descend(node.right))
        if isinstance(node, ast.BoolOp):
            kind = "and" if isinstance(node.op, ast.And) else "or"
            values = [descend(value) for value in node.values]
            result = values[0]
            for value in values[1:]:
                result = _binary(kind, result, value)
            return result
        if isinstance(node, ast.Compare):
            comparisons = []
            left = node.left
            for op, right in zip(node.ops, node.comparators):
                if type(op) not in COMPARISONS:
                    raise ContractError("unsupported comparison operator")
                comparisons.append(_binary(COMPARISONS[type(op)], descend(left), descend(right)))
                left = right
            result = comparisons[0]
            for comparison in comparisons[1:]:
                result = _binary("and", result, comparison)
            return result
        raise ContractError(f"unsupported law syntax: {type(node).__name__}")

    return visit(tree.body)


def unchanged_expression(path: str) -> dict:
    if not isinstance(path, str) or not path:
        raise ContractError("unchanged fields must be nonempty dotted paths")
    return compile_expression(f"after.{path} == before.{path}")
