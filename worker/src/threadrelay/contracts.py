"""Schema-backed validation for private, untrusted protocol data."""
from functools import lru_cache
from importlib.resources import files
import json
from typing import Any, TypeAlias

from jsonschema import Draft7Validator, FormatChecker


# Named JSON contracts are schema-validated before use.
Command: TypeAlias = dict[str, Any]
Event: TypeAlias = dict[str, Any]


class ProtocolError(ValueError):
    """The input was rejected before any command handler could run."""


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    schema = json.loads(files("threadrelay").joinpath("protocol.schema.json").read_text())
    Draft7Validator.check_schema(schema)
    return schema


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft7Validator:
    schema = _schema()
    if name not in schema["definitions"]:
        raise ProtocolError("Unknown contract")
    selected = {**schema, "$ref": f"#/definitions/{name}"}
    return Draft7Validator(selected, format_checker=FormatChecker())


def validate_contract(name: str, value: Any) -> None:
    """Validate without coercion or default mutation; errors contain no user content."""
    try:
        if not _validator(name).is_valid(value):
            raise ProtocolError(f"Invalid {name}")
    except (RecursionError, TypeError) as error:
        raise ProtocolError("Invalid protocol value") from error
