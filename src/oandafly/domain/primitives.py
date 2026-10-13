"""Canonical financial values and unambiguous wire encodings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType, UnionType
from typing import Annotated, Any, Literal, Never, Self, Union, get_args, get_origin
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
    SerializerFunctionWrapHandler,
    ValidationInfo,
    field_serializer,
    model_validator,
)

from oandafly.domain.errors import ValidationFailure


def money(value: str | Decimal | int) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, Decimal, int)):
        raise ValidationFailure("Financial amounts must be decimal strings or integers")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValidationFailure("Invalid decimal amount") from exc
    if not result.is_finite():
        raise ValidationFailure("Financial amounts must be finite")
    exponent = result.as_tuple().exponent
    if not isinstance(exponent, int) or len(result.as_tuple().digits) > 80 or abs(exponent) > 80:
        raise ValidationFailure("Decimal precision exceeds the application bound")
    return result


def decimal_text(value: Decimal) -> str:
    checked = money(value)
    return format(checked, "f") if checked else "0"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailure("An explicit UTC offset is required")
    return value.astimezone(timezone.utc)


def _encode(value: object) -> object:
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, datetime):
        return require_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Unsupported canonical type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(value, default=_encode, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _check_numeric(value: Any) -> None:
    if isinstance(value, float):
        raise ValueError("Floating point values are not permitted in domain models")
    if isinstance(value, Decimal):
        money(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            _check_numeric(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _check_numeric(item)


class FrozenDict(Mapping[str, Any]):
    """Detached mapping backed by a private read-only proxy, never a dict subclass.

    Dict base-class mutators cannot write this mapping.
    Serialization creates new wire containers without exposing the backing store.
    """

    __slots__ = ("_values",)
    _values: Mapping[str, Any]

    def __init__(self, values: Mapping[str, Any]) -> None:
        if hasattr(self, "_values"):
            raise TypeError("Domain mappings are immutable")
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def _immutable(self, *args: Any, **kwargs: Any) -> Never:
        raise TypeError("Domain mappings are immutable")

    __setattr__ = _immutable
    __delattr__ = _immutable
    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        return self


def _wire_containers(value: Any) -> Any:
    """Detach containers only; scalar encodings remain Pydantic's responsibility."""
    if isinstance(value, Mapping):
        return {key: _wire_containers(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_wire_containers(item) for item in value)
    if isinstance(value, list):
        return [_wire_containers(item) for item in value]
    return value


def freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, Decimal, date, UUID, Enum, bytes, SecretStr)):
        return value
    if isinstance(value, StrictModel):
        return value
    raise ValueError("Unsupported mutable domain value")


def _wire_value(value: Any, annotation: Any) -> Any:
    """Only named wire encodings are converted; never bool/int/string scalars."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        return _wire_value(value, args[0])
    if origin in (Union, UnionType):
        if value is None:
            return None
        concrete = tuple(arg for arg in args if arg is not type(None))
        return _wire_value(value, concrete[0]) if len(concrete) == 1 else value
    if origin is tuple and isinstance(value, (list, tuple)):
        if not args or (args[-1] is not Ellipsis and len(value) != len(args)):
            return tuple(value)
        return tuple(_wire_value(item, args[0] if len(args) == 2 and args[1] is Ellipsis else args[index])
                     for index, item in enumerate(value))
    if origin in (dict, Mapping) and isinstance(value, Mapping) and len(args) == 2:
        return {key: _wire_value(item, args[1]) for key, item in value.items()}
    if annotation is Decimal and isinstance(value, (str, int)):
        return money(value)
    if annotation is datetime and isinstance(value, str):
        return require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    if annotation is date and isinstance(value, str):
        return date.fromisoformat(value)
    if annotation is UUID and isinstance(value, str):
        return UUID(value)
    if isinstance(annotation, type) and issubclass(annotation, Enum) and isinstance(value, str):
        return annotation(value)
    return value


def _domain_scalars(value: Any, annotation: Any) -> None:
    """Field validators cannot silently weaken the structural scalar contract."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        _domain_scalars(value, args[0])
    elif origin in (Union, UnionType):
        concrete = tuple(arg for arg in args if arg is not type(None))
        if value is not None and len(concrete) == 1:
            _domain_scalars(value, concrete[0])
    elif origin is Literal and not any(type(value) is type(item) and value == item for item in args):
        raise ValueError("Literal values require their exact scalar type")
    elif annotation in (str, bool, int, Decimal) and type(value) is not annotation:
        raise ValueError("Domain scalar type mismatch; decode raw inputs through from_wire")
    elif origin is tuple and isinstance(value, (list, tuple)) and args:
        for index, item in enumerate(value):
            if len(args) == 2 and args[1] is Ellipsis:
                _domain_scalars(item, args[0])
            elif index < len(args):
                _domain_scalars(item, args[index])
    elif origin in (dict, Mapping) and isinstance(value, Mapping) and len(args) == 2:
        for key, item in value.items():
            _domain_scalars(key, args[0])
            _domain_scalars(item, args[1])


class StrictModel(BaseModel):
    """Immutable, closed domain schema with exact JSON decimal serialization."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False,
                              validate_default=True, revalidate_instances="always", hide_input_in_errors=True)

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> Self:
        """Explicit JSON boundary: decimal/date/UUID strings and arrays are wire types.

        Strict JSON validation still rejects numeric/boolean/string coercion.
        Duplicate keys and size limits are checked by the ingress codec first.
        """
        try:
            _check_numeric(value)
        except ValueError:
            raise ValidationFailure("Invalid numeric wire value") from None
        return cls.model_validate_json(canonical_json(value))

    @model_validator(mode="before")
    @classmethod
    def reject_approximate_financial_values(cls, value: Any, info: ValidationInfo) -> Any:
        _check_numeric(value)
        # Pydantic Literal[True]/Literal[False] otherwise accept integer 1/0.
        if isinstance(value, Mapping):
            for name, field in cls.model_fields.items():
                if name in value and str(get_origin(field.annotation)) == "typing.Literal":
                    alternatives = get_args(field.annotation)
                    if any(type(item) is bool for item in alternatives) and type(value[name]) is not bool:
                        raise ValueError("Boolean literals require actual booleans")
            if info.mode == "json":
                value = {name: _wire_value(item, cls.model_fields[name].annotation)
                         if name in cls.model_fields else item for name, item in value.items()}
            for name, item in value.items():
                if name in cls.model_fields:
                    _domain_scalars(item, cls.model_fields[name].annotation)
        return _wire_containers(value)

    @field_serializer("*", mode="wrap", check_fields=False)
    def serialize_children(self, value: Any, handler: SerializerFunctionWrapHandler) -> Any:
        return handler(_wire_containers(value))

    @model_validator(mode="after")
    def immutable_children(self) -> Self:
        for name in type(self).model_fields:
            object.__setattr__(self, name, freeze(getattr(self, name)))
        return self
