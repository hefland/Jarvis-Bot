"""Exact math using SymPy. Runs in a separate process (see calculator.py).

Accepts normal math plus SymPy functions, e.g.
    2^10, 5!, sqrt(2)*3, 15% * 80, sin(30 deg)
    solve(x^2 - 5x + 6 = 0, x)
    diff(sin(x)*x^2, x)          integrate(x^2, (x, 0, 3))
    limit(sin(x)/x, x, 0)        factor(x^2 - 4)
    det([[1,2],[3,4]])           mean(3, 5, 10)
"""
from __future__ import annotations

import re
import statistics
from typing import Any

MAX_LEN = 500

FUNCTIONS = {
    # basic / algebra
    "sqrt", "cbrt", "root", "exp", "log", "ln", "abs", "Abs", "floor", "ceiling", "sign", "Mod", "mod",
    "factorial", "binomial", "gamma", "gcd", "lcm", "isprime", "prime", "factorint", "primefactors",
    "divisors", "Rational", "Integer", "Float", "N", "re", "im", "conjugate", "arg", "Max", "Min", "max", "min", "round",
    # trig
    "sin", "cos", "tan", "cot", "sec", "csc", "asin", "acos", "atan", "acot", "atan2",
    "sinh", "cosh", "tanh", "asinh", "acosh", "atanh",
    # calculus / algebra operations
    "diff", "integrate", "limit", "solve", "nsolve", "simplify", "expand", "factor", "together", "apart",
    "cancel", "trigsimp", "nsimplify", "summation", "product", "series", "Sum", "Product", "Eq", "Derivative",
    "Integral", "roots", "dsolve", "Function",
    # matrices
    "Matrix", "det", "inverse", "transpose", "eigenvals", "rank",
    # statistics
    "mean", "median", "mode", "stdev", "pstdev", "variance", "pvariance", "total",
    # constants
    "pi", "E", "I", "oo", "inf", "infinity", "deg", "degrees", "radians", "golden_ratio", "GoldenRatio",
    # helpers that sympy needs internally for implicit multiplication
    "Symbol", "Number", "Pow", "Mul", "Add",
}
GREEK = {"alpha", "beta", "gamma", "delta", "epsilon", "theta", "lambda_", "mu", "sigma", "tau", "phi", "omega", "rho"}
_IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


class MathInputError(ValueError):
    pass


def _check_safe(expr: str) -> None:
    if len(expr) > MAX_LEN:
        raise MathInputError("expression is too long")
    if "__" in expr or "'" in expr or '"' in expr or "\\" in expr or ";" in expr or "lambda " in expr:
        raise MathInputError("expression contains characters that aren't math")
    if re.search(r"[A-Za-z_)\]]\s*\.\s*[A-Za-z_]", expr):
        raise MathInputError("expression contains characters that aren't math")
    for name in _IDENT.findall(expr):
        if name in FUNCTIONS or name in GREEK or len(name) <= 2:
            continue
        raise MathInputError(f"unknown word '{name}' in the math expression")


def _prepare(expr: str) -> str:
    e = expr.strip()
    e = e.replace("×", "*").replace("÷", "/").replace("−", "-").replace("^", "**").replace("√", "sqrt")
    e = e.replace("π", "pi").replace("°", " deg")
    e = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", e)  # 15% -> (15/100)
    e = re.sub(r"\binfinity\b|\binf\b", "oo", e)
    e = re.sub(r"\s+mod(?:ulo)?\s+", " % ", e)  # "7 mod 3" -> 7 % 3
    # turn "a = b" (a single equals) into Eq(a, b) at the top level of solve(...) or alone
    return e


def _split_top_level(s: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _equals_to_eq(e: str) -> str:
    """Convert 'lhs = rhs' to 'Eq(lhs, rhs)' (also inside each top-level argument of solve)."""
    def conv(piece: str) -> str:
        if re.search(r"(?<![<>=!])=(?!=)", piece):
            lhs, rhs = re.split(r"(?<![<>=!])=(?!=)", piece, maxsplit=1)
            return f"Eq({lhs.strip()}, {rhs.strip()})"
        return piece

    m = re.match(r"^\s*(solve|nsolve|dsolve)\s*\((.*)\)\s*$", e, re.DOTALL)
    if m:
        args = _split_top_level(m.group(2))
        inner = args[0]
        # list of equations: [x+y=3, x-y=1]
        if inner.strip().startswith("[") and inner.strip().endswith("]"):
            eqs = _split_top_level(inner.strip()[1:-1])
            inner = "[" + ", ".join(conv(x) for x in eqs) + "]"
        else:
            inner = conv(inner)
        return f"{m.group(1)}({', '.join([inner] + args[1:])})"
    if "=" in e and not e.lstrip().startswith(("Eq(",)):
        # a bare equation like "2x + 3 = 11" means: solve it
        if re.search(r"(?<![<>=!])=(?!=)", e):
            return f"solve({conv(e)})"
    return e


def _namespace() -> dict[str, Any]:
    import sympy as sp

    ns: dict[str, Any] = {}
    exec("from sympy import *", ns)  # noqa: S102 - fixed import, not user input
    ns.pop("__builtins__", None)

    def _nums(args):
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            args = args[0]
        return [sp.nsimplify(a) if not isinstance(a, sp.Basic) else a for a in args]

    def _stat(fn):
        def inner(*args):
            vals = _nums(args)
            try:
                return sp.nsimplify(fn([sp.Rational(v) if v.is_Rational else v for v in vals]))
            except Exception:
                return sp.Float(fn([float(v) for v in vals]))
        return inner

    ns.update({
        "ln": sp.log, "abs": sp.Abs, "mod": sp.Mod, "max": sp.Max, "min": sp.Min,
        "inf": sp.oo, "infinity": sp.oo, "golden_ratio": sp.GoldenRatio,
        "deg": sp.pi / 180, "degrees": lambda r: r * 180 / sp.pi, "radians": lambda d: d * sp.pi / 180,
        "cbrt": lambda v: sp.real_root(v, 3), "lambda_": sp.Symbol("lambda"),
        "det": lambda m: sp.Matrix(m).det(), "inverse": lambda m: sp.Matrix(m).inv(),
        "transpose": lambda m: sp.Matrix(m).T, "eigenvals": lambda m: sp.Matrix(m).eigenvals(),
        "rank": lambda m: sp.Matrix(m).rank(),
        "mean": _stat(statistics.mean), "median": _stat(statistics.median), "mode": _stat(statistics.mode),
        "stdev": _stat(statistics.stdev), "pstdev": _stat(statistics.pstdev),
        "variance": _stat(statistics.variance), "pvariance": _stat(statistics.pvariance),
        "total": lambda *a: sp.Add(*_nums(a)),
        "gamma": sp.gamma, "e": sp.E,
        "round": lambda v, n=0: sp.Integer(round(float(v))) if int(n) == 0 else sp.Rational(str(round(float(v), int(n)))),
    })
    return ns


def _fmt_number(value) -> str:
    import sympy as sp
    approx = sp.N(value, 15)
    if approx.is_Integer or (approx.is_Number and approx == int(approx) and abs(approx) < 10**15):
        return str(int(approx))
    if approx.is_real:
        f = float(approx)
        if abs(f) >= 1e15 or (0 < abs(f) < 1e-6):
            return f"{f:.10e}"
        return f"{f:.12g}"
    return str(approx)


def _describe(result) -> dict[str, Any]:
    import sympy as sp

    if isinstance(result, bool) or result in (sp.true, sp.false):
        return {"exact": str(bool(result))}
    if isinstance(result, dict):
        return {"exact": {str(k): str(v) for k, v in result.items()},
                "decimal": {str(k): _fmt_number(v) if getattr(v, "is_number", False) else str(v)
                            for k, v in result.items()}}
    if isinstance(result, (list, tuple, set)):
        items = list(result)
        if not items:
            return {"exact": "no solution"}
        return {"exact": [str(r) for r in items],
                "decimal": [_fmt_number(r) if getattr(r, "is_number", False) else
                            (str(r) if not isinstance(r, (dict, tuple)) else str(r)) for r in items]}
    if isinstance(result, sp.MatrixBase):
        return {"exact": str(result.tolist())}
    if isinstance(result, sp.Basic):
        out: dict[str, Any] = {"exact": str(result)}
        if result.is_number:
            out["decimal"] = _fmt_number(result)
        return out
    return {"exact": str(result)}


_OPERATIONS = {"factor", "expand", "apart", "together", "cancel", "series", "trigsimp", "nsimplify",
               "diff", "integrate", "solve", "dsolve", "roots", "factorint", "Matrix"}


def _explicit_operation(prepared: str) -> bool:
    m = re.match(r"^\s*([A-Za-z_]+)\s*\(", prepared)
    return bool(m and m.group(1) in _OPERATIONS)


def _pretty(obj):
    if isinstance(obj, str):
        return obj.replace("**", "^")
    if isinstance(obj, list):
        return [_pretty(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _pretty(v) for k, v in obj.items()}
    return obj


def evaluate(expression: str) -> dict[str, Any]:
    """Evaluate an expression and return {'input', 'exact', 'decimal'} or {'error'}."""
    import sympy as sp
    from sympy.parsing.sympy_parser import (convert_xor, implicit_multiplication_application,
                                            parse_expr, rationalize, standard_transformations)

    try:
        _check_safe(expression)
        prepared = _equals_to_eq(_prepare(expression))
        ns = _namespace()
        transformations = standard_transformations + (implicit_multiplication_application, convert_xor,
                                                       rationalize)
        result = parse_expr(prepared, local_dict={}, global_dict=ns, transformations=transformations,
                            evaluate=True)
        if isinstance(result, (sp.Integral, sp.Derivative, sp.Limit, sp.Sum, sp.Product)):
            result = result.doit()
        elif isinstance(result, sp.Basic) and not result.is_Atom and not _explicit_operation(prepared):
            simplified = sp.simplify(result) if len(str(result)) < 200 else result
            result = simplified
        if result in (sp.zoo, sp.nan) or (isinstance(result, sp.Basic) and result.has(sp.zoo, sp.nan)):
            return {"input": prepared.replace("**", "^"), "exact": "undefined (for example, division by zero)"}
        out = {"input": prepared.replace("**", "^")}
        out.update(_describe(result))
        return _pretty(out)
    except MathInputError as exc:
        return {"error": str(exc)}
    except (SyntaxError, TypeError, ValueError, ZeroDivisionError, NotImplementedError,
            AttributeError, sp.SympifyError) as exc:
        return {"error": f"could not compute that ({type(exc).__name__}: {exc})"}


def convert(value: float, from_unit: str, to_unit: str) -> dict[str, Any]:
    import pint

    ureg = _ureg()
    try:
        q = ureg.Quantity(float(value), _unit_alias(from_unit)).to(_unit_alias(to_unit))
    except (pint.errors.UndefinedUnitError, pint.errors.DimensionalityError, ValueError) as exc:
        return {"error": str(exc)}
    mag = q.magnitude
    pretty = f"{mag:.10g}"
    return {"input": f"{value} {from_unit}", "result": f"{pretty} {to_unit}", "value": float(f"{mag:.12g}")}


_UREG = None


def _ureg():
    global _UREG
    if _UREG is None:
        import pint
        _UREG = pint.UnitRegistry(autoconvert_offset_to_baseunit=True)
    return _UREG


_ALIASES = {
    "f": "degF", "fahrenheit": "degF", "degrees fahrenheit": "degF", "°f": "degF",
    "c": "degC", "celsius": "degC", "degrees celsius": "degC", "°c": "degC", "centigrade": "degC",
    "k": "kelvin", "mph": "mile/hour", "kph": "kilometer/hour", "km/h": "kilometer/hour", "kmh": "kilometer/hour",
    "cups": "cup", "tbsp": "tablespoon", "tsp": "teaspoon", "lbs": "pound", "lb": "pound", "oz": "ounce",
    "fl oz": "fluid_ounce", "fluid ounces": "fluid_ounce", "fluid ounce": "fluid_ounce",
    "stone": "stone", "mpg": "mile/gallon",
}


def _unit_alias(unit: str) -> str:
    u = unit.strip()
    return _ALIASES.get(u.lower(), u)
