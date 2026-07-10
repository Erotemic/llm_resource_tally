# SPDX-License-Identifier: Apache-2.0
"""Small non-negative interval arithmetic for transparent scenario bounds."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Interval:
    low: float
    central: float
    high: float

    def __post_init__(self):
        vals = tuple(float(v) for v in (self.low, self.central, self.high))
        if any(v < 0 for v in vals) or not vals[0] <= vals[1] <= vals[2]:
            raise ValueError(f"invalid non-negative interval: {vals}")
        object.__setattr__(self, "low", vals[0])
        object.__setattr__(self, "central", vals[1])
        object.__setattr__(self, "high", vals[2])

    @classmethod
    def exact(cls, value: float) -> "Interval":
        return cls(value, value, value)

    @classmethod
    def coerce(cls, value) -> "Interval":
        if isinstance(value, cls):
            return value
        if isinstance(value, (int, float)):
            return cls.exact(float(value))
        if isinstance(value, (list, tuple)) and len(value) == 3:
            return cls(*map(float, value))
        if isinstance(value, dict) and {"low", "central", "high"} <= set(value):
            return cls(value["low"], value["central"], value["high"])
        raise ValueError(f"expected scalar or [low, central, high], got {value!r}")

    def __add__(self, other) -> "Interval":
        other = Interval.coerce(other)
        return Interval(self.low + other.low, self.central + other.central,
                        self.high + other.high)

    def __radd__(self, other) -> "Interval":
        return self + other

    def __mul__(self, other) -> "Interval":
        other = Interval.coerce(other)
        return Interval(self.low * other.low, self.central * other.central,
                        self.high * other.high)

    def __rmul__(self, other) -> "Interval":
        return self * other

    def __truediv__(self, other) -> "Interval":
        other = Interval.coerce(other)
        if other.low <= 0:
            raise ValueError(f"interval divisor must be positive, got {other}")
        return Interval(self.low / other.high, self.central / other.central,
                        self.high / other.low)

    def scaled(self, value: float) -> "Interval":
        return self * value

    def to_dict(self, unit: str | None = None, digits: int = 12) -> dict:
        out = {"low": round(self.low, digits), "central": round(self.central, digits),
               "high": round(self.high, digits)}
        if unit:
            out["unit"] = unit
        return out


def contains_interval(value) -> bool:
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return True
    if isinstance(value, dict):
        if {"low", "central", "high"} <= set(value):
            return True
        return any(contains_interval(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_interval(v) for v in value)
    return False


ZERO = Interval.exact(0.0)
