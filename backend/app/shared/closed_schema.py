"""Closed answer spaces for model replies: reply schemas built for one call, whose fields are
enum lists of the only values the call may return. The provider constrains its output to the
schema and the gateway rejects anything outside it."""

from collections.abc import Sequence
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, Field, WithJsonSchema, create_model

ModelT = TypeVar("ModelT", bound=BaseModel)


def one_of(values: Sequence[str | int]) -> Any:
    """A type that accepts exactly these values. Its JSON schema is always an enum (pydantic
    would write a single value as const)."""
    kind = "integer" if all(isinstance(v, int) for v in values) else "string"
    allowed = Literal.__getitem__(tuple(values))  # Literal["a", "b"], built at run time
    return Annotated[allowed, WithJsonSchema({"type": kind, "enum": list(values)})]


def list_of(values: Sequence[str | int]) -> Any:
    """A list of the given values; a list that must stay empty when there are none."""
    if not values:
        return Annotated[list[str], Field(max_length=0)]
    return list.__class_getitem__(one_of(values))  # list[<the enum>], built at run time


def closed_schema(base: type[ModelT], name: str, **fields: Any) -> type[ModelT]:
    """A subclass of base whose named fields are replaced, for one call."""
    return create_model(name, __base__=base, **fields)
