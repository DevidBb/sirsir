"""Безопасный калькулятор: разбор выражения через ast, без eval.

Разрешены только числа, + - * / // % **, скобки и несколько функций.
Русские фразы («15 процентов от 2400», «25 умножить на 4», «корень из 144»)
переводятся в выражение перед вычислением.
"""

from __future__ import annotations

import ast
import math
import operator
import re

from .reminders import parse_number
from .tool_registry import Param, Tool, ToolRegistry

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
           ast.Pow: operator.pow}
_UNOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"sqrt": math.sqrt, "abs": abs, "round": round, "sin": lambda x: math.sin(math.radians(x)),
          "cos": lambda x: math.cos(math.radians(x)), "log": math.log10, "ln": math.log}
MAX_POWER = 1000
MAX_LEN = 200


class CalcError(ValueError):
    pass


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > MAX_POWER or abs(left) > 10**6):
            raise CalcError("Слишком большая степень")
        try:
            return _BINOPS[type(node.op)](left, right)
        except ZeroDivisionError:
            raise CalcError("Деление на ноль") from None
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNOPS:
        return _UNOPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in _FUNCS and len(node.args) == 1 and not node.keywords:
        try:
            return _FUNCS[node.func.id](_eval(node.args[0]))
        except ValueError:
            raise CalcError("Недопустимый аргумент функции") from None
    raise CalcError("Недопустимое выражение")


def evaluate(expression: str) -> float:
    expr = expression.strip().replace(",", ".").replace("×", "*").replace("÷", "/").replace("^", "**")
    if not expr or len(expr) > MAX_LEN:
        raise CalcError("Пустое или слишком длинное выражение")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        raise CalcError("Не понял выражение") from None
    result = _eval(tree)
    if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
        raise CalcError("Результат не определён")
    return result


def format_number(value: float) -> str:
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer() and abs(value) < 1e15):
        return str(int(value))
    text = f"{value:.6g}"
    if "e" in text:
        return text
    return text.replace(".", ",")


# --- русские фразы → выражение ---
_WORD_OPS = [
    (r"\bумножить на\b|\bумноженное на\b|\bпомножить на\b|\bраз\b|\bх\b", "*"),
    (r"\bразделить на\b|\bподелить на\b|\bделить на\b|\bделённое на\b|\bделенное на\b", "/"),
    (r"\bплюс\b|\bприбавить\b", "+"),
    (r"\bминус\b|\bотнять\b", "-"),
    (r"\bв квадрате\b", "**2"),
    (r"\bв кубе\b", "**3"),
    (r"\bв (\d+)(?:-?(?:й|ой|ей))? степени\b", r"** \1"),   # «2 в 10 степени»
    (r"\bв степени\b", "**"),
]
_PERCENT_OF = re.compile(r"^([\d.,]+)\s*(?:%|процент(?:а|ов)?)\s+от\s+([\d.,]+)$")
_ROOT = re.compile(r"^(?:квадратный )?корень (?:из )?([\d.,]+)$")


def _numbers_to_digits(text: str) -> str:
    """«двадцать пять» → «25» (пока подряд идут числительные)."""
    words, out, buf = text.split(), [], []
    for w in words + [""]:
        candidate = " ".join(buf + [w]) if w else None
        if candidate and parse_number(candidate) is not None and not w.isdigit():
            buf.append(w)
            continue
        if buf:
            out.append(str(parse_number(" ".join(buf))))
            buf = []
        if w:
            out.append(w)
    return " ".join(out)


def phrase_to_expression(phrase: str) -> str | None:
    t = phrase.lower().replace("ё", "е").strip(" ?.!")
    t = re.sub(r"^(?:сколько будет|посчитай|вычисли|сколько)\s+", "", t)
    t = _numbers_to_digits(t)
    m = _PERCENT_OF.match(t)
    if m:
        return f"{m.group(2)}*{m.group(1)}/100"
    m = _ROOT.match(t)
    if m:
        return f"sqrt({m.group(1)})"
    for pattern, op in _WORD_OPS:
        t = re.sub(pattern, f" {op} ", t)  # op может ссылаться на группу: «** \1»
    t = re.sub(r"\s+", "", t).replace(",", ".")
    if not re.fullmatch(r"[\d.+\-*/()]+", t) or not re.search(r"\d", t) or not re.search(r"[+\-*/]", t):
        return None
    return t


def calculate(expression: str) -> str:
    try:
        return f"Получается {format_number(evaluate(expression))}."
    except CalcError as e:
        return f"Не могу посчитать: {e}."


def register_tools(registry: ToolRegistry) -> None:
    registry.register(Tool(
        "calculate",
        "Посчитать арифметическое выражение (числа, + - * / ** %, скобки, sqrt). "
        "Используй для любых вычислений. Не запускает программу «Калькулятор» — для неё open_app.",
        calculate,
        {"expression": Param("string", "Выражение, например 2400*15/100", max_length=MAX_LEN)}))
