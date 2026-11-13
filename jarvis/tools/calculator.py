"""Calculator + unit converter tools. The math runs in a separate process with a time limit
so a very hard problem can never freeze the assistant."""
from __future__ import annotations

import logging
import multiprocessing as mp
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from concurrent.futures.process import BrokenProcessPool

from . import math_engine, tool

log = logging.getLogger(__name__)
TIME_LIMIT = 15.0


class _Sandbox:
    def __init__(self):
        self._lock = threading.Lock()
        self._pool: ProcessPoolExecutor | None = None

    def _get_pool(self) -> ProcessPoolExecutor:
        if self._pool is None:
            self._pool = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))
        return self._pool

    def _kill(self) -> None:
        if self._pool is not None:
            for proc in list(getattr(self._pool, "_processes", {}).values()):
                proc.kill()
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def run(self, fn, *args, timeout: float = TIME_LIMIT):
        with self._lock:
            for attempt in range(2):
                try:
                    future = self._get_pool().submit(fn, *args)
                    return future.result(timeout=timeout)
                except FutureTimeout:
                    self._kill()
                    return {"error": f"that calculation took longer than {int(timeout)} seconds, so I stopped it"}
                except BrokenProcessPool:
                    self._kill()
                    if attempt == 1:
                        raise
            return {"error": "the math engine crashed"}

    def warm_up(self) -> None:
        threading.Thread(target=lambda: self.run(math_engine.evaluate, "1+1", timeout=60), daemon=True).start()


SANDBOX = _Sandbox()


@tool(
    "calculate",
    "Exact calculator. ALWAYS use this for ANY arithmetic or math - never compute in your head. "
    "Write the math in Python/SymPy style. Examples: '2^10', '15% * 80', 'sqrt(2)*3', '5!', 'sin(30 deg)', "
    "'solve(x^2 - 5x + 6 = 0, x)', 'solve([x + y = 10, x - y = 2], [x, y])', 'diff(x^3, x)', "
    "'integrate(x^2, (x, 0, 3))', 'limit(sin(x)/x, x, 0)', 'factor(x^2 - 4)', 'mean(3, 5, 10)', "
    "'det([[1,2],[3,4]])', '1234 * 5678'. Returns the exact answer and a decimal.",
    {"expression": {"type": "string", "description": "The math to compute."}},
    ["expression"],
)
def calculate(settings, expression: str):
    return SANDBOX.run(math_engine.evaluate, str(expression))


@tool(
    "convert_units",
    "Convert between units exactly (length, weight, volume, cooking, temperature, speed, area, energy, data, time). "
    "Examples: 5 miles -> km, 350 fahrenheit -> celsius, 2 cups -> ml, 180 lb -> kg, 1 gigabyte -> megabyte.",
    {
        "value": {"type": "number", "description": "The amount, e.g. 5"},
        "from_unit": {"type": "string", "description": "Unit to convert from, e.g. 'miles'"},
        "to_unit": {"type": "string", "description": "Unit to convert to, e.g. 'kilometers'"},
    },
    ["value", "from_unit", "to_unit"],
)
def convert_units(settings, value, from_unit: str, to_unit: str):
    return SANDBOX.run(math_engine.convert, float(value), str(from_unit), str(to_unit))
