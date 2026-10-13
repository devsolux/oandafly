"""Closed numerical-model schemas, deliberately separate from financial contracts.

Floating point is permitted only here and in neural feature/action data. Monetary
amounts, native quantities, prices and authorization limits retain StrictModel.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_serializer, model_validator

from oandafly.domain.primitives import FrozenDict, canonical_json, _wire_containers, _wire_value


def numerical_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenDict({key: numerical_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(numerical_freeze(item) for item in value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite numerical value")
        return value
    if value is None or isinstance(value, (str, bool, int, Decimal, date, Enum, BaseModel)):
        return value
    raise ValueError("Unsupported numerical artifact value")


class NumericModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True,
                              allow_inf_nan=False, validate_default=True,
                              revalidate_instances="always", hide_input_in_errors=True)

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> Self:
        return cls.model_validate_json(canonical_json(value))

    @model_validator(mode="before")
    @classmethod
    def detached_containers(cls, value: Any, info: ValidationInfo) -> Any:
        if info.mode == "json" and isinstance(value, Mapping):
            value = {name: _wire_value(item, cls.model_fields[name].annotation)
                     if name in cls.model_fields else item for name, item in value.items()}
        return _wire_containers(value)

    @field_serializer("*", mode="wrap", check_fields=False)
    def serialize_children(self, value: Any, handler: Any) -> Any:
        return handler(_wire_containers(value))

    @model_validator(mode="after")
    def immutable_children(self) -> Self:
        for name in type(self).model_fields:
            object.__setattr__(self, name, numerical_freeze(getattr(self, name)))
        return self
