"""Owned arithmetic policies; no mutable Decimal context escapes this module."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import (
    ROUND_HALF_EVEN,
    Clamped,
    Context,
    DivisionByZero,
    FloatOperation,
    InvalidOperation,
    Overflow,
    Underflow,
    localcontext,
)
from functools import wraps
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class FinancialContext:
    """Fixed policy. Each invocation creates its own context, flags and traps.

    Input amounts have at most 80 digits and exponents in [-80, 80]. 1024
    significant digits cover sums/differences of up to four bounded factors
    (at most 957 places across the permitted exponent span). Recurring division
    intentionally rounds HALF_EVEN; unit lattice rounding remains toward zero.
    """

    @contextmanager
    def local(self) -> Iterator[Context]:
        owned = Context(prec=1024, rounding=ROUND_HALF_EVEN, Emin=-9999, Emax=9999,
                        capitals=1, clamp=0, flags=[], traps=[InvalidOperation,
                        DivisionByZero, Overflow, Underflow, Clamped, FloatOperation])
        with localcontext(owned) as active:
            yield active

    def calculation(self, function: Callable[P, R]) -> Callable[P, R]:
        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with self.local():
                return function(*args, **kwargs)
        return wrapped


FINANCIAL = FinancialContext()
