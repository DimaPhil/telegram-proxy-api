from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from typing import TypeAlias, cast

from .errors import CliError

Json: TypeAlias = str | int | float | bool | None | list["Json"] | dict[str, "Json"]
JsonObject: TypeAlias = dict[str, Json]

COMMANDS: dict[tuple[str, str], tuple[str, ...]] = {
    ("GET", "/healthz"): ("health",),
    ("GET", "/me"): ("me",),
    ("GET", "/contacts"): ("contacts", "list"),
    ("GET", "/resolve"): ("resolve",),
    ("GET", "/chats"): ("chats", "list"),
    ("GET", "/chats/{chat_id}"): ("chats", "get"),
    ("GET", "/chats/{chat_id}/messages"): ("messages", "list"),
    ("GET", "/chats/{chat_id}/messages/{message_id}"): ("messages", "get"),
    ("GET", "/chats/{chat_id}/messages/{message_id}/context"): ("messages", "context"),
    ("GET", "/messages/search"): ("messages", "search"),
    ("GET", "/chats/{chat_id}/messages/{message_id}/media"): ("media", "list"),
    ("GET", "/chats/{chat_id}/messages/{message_id}/media/{media_id}"): ("media", "download"),
    ("GET", "/chats/{chat_id}/messages/{message_id}/media/bundle"): ("media", "bundle"),
}


def object_value(value: Json, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise CliError(f"Invalid {label}: expected an object.")
    return value


def load_schema() -> JsonObject:
    value = cast(Json, json.loads(files("telegram_proxy_cli").joinpath("openapi.json").read_text()))
    return object_value(value, "OpenAPI document")


@dataclass(frozen=True)
class Operation:
    id: str
    method: str
    path: str
    document: JsonObject
    parameters: tuple[JsonObject, ...]

    @property
    def binary(self) -> bool:
        responses = object_value(self.document.get("responses", {}), "responses")
        for code, response in responses.items():
            if not code.startswith("2"):
                continue
            content = object_value(object_value(response, "response").get("content", {}), "response content")
            for media_type, media in content.items():
                schema = object_value(object_value(media, "media type").get("schema", {}), "response schema")
                if schema.get("format") == "binary" or not (
                    media_type == "application/json" or media_type.endswith("+json")
                ):
                    return True
        return False


def operations(schema: JsonObject | None = None) -> list[Operation]:
    schema = schema if schema is not None else load_schema()
    paths = object_value(schema.get("paths"), "OpenAPI paths")
    result: list[Operation] = []
    for path, item in paths.items():
        path_item = object_value(item, "path item")
        for method, definition in path_item.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                continue
            document = object_value(definition, "operation")
            identifier = document.get("operationId")
            if not isinstance(identifier, str):
                raise CliError(f"Operation {method} {path} requires operationId.")
            parameters: list[JsonObject] = []
            for source in (path_item.get("parameters", []), document.get("parameters", [])):
                if not isinstance(source, list):
                    raise CliError("Invalid OpenAPI parameter list.")
                parameters.extend(object_value(value, "parameter") for value in source)
            result.append(Operation(identifier, method.upper(), path, document, tuple(parameters)))
    if len({operation.id for operation in result}) != len(result):
        raise CliError("OpenAPI operation IDs must be unique.")
    for operation in result:
        validate_contract(operation)
    return sorted(result, key=lambda operation: operation.id)


def validate_contract(operation: Operation) -> None:
    """Fail CI and loading rather than silently shipping an unusable contract."""
    names: set[str] = set()
    for parameter in operation.parameters:
        name = parameter.get("name")
        if not isinstance(name, str) or name in names:
            raise CliError(f"Missing or duplicate parameter name in {operation.id}.")
        names.add(name)
        if parameter.get("in") not in {"path", "query"}:
            raise CliError(f"Unsupported parameter location in {operation.id}: {name}.")
        definition = object_value(parameter.get("schema", {}), "parameter schema")
        alternatives = definition.get("anyOf")
        if isinstance(alternatives, list):
            non_null = [
                object_value(item, "parameter alternative")
                for item in alternatives
                if isinstance(item, dict) and item.get("type") != "null"
            ]
            if len(non_null) != 1:
                raise CliError(f"Unsupported parameter union in {operation.id}: {name}.")
            definition = non_null[0]
        if definition.get("type") not in {"string", "integer", "number", "boolean"}:
            raise CliError(f"Unsupported parameter schema in {operation.id}: {name}.")
        if "$ref" in definition or "oneOf" in definition or "allOf" in definition:
            raise CliError(f"Unsupported parameter schema composition in {operation.id}: {name}.")
    body = operation.document.get("requestBody")
    if body is not None:
        definition = object_value(body, "request body")
        content = object_value(definition.get("content", {}), "request body content")
        if set(content) != {"application/json"}:
            raise CliError(f"Only JSON request bodies are supported: {operation.id}.")


def parameter_type(parameter: JsonObject) -> str:
    schema = object_value(parameter.get("schema", {}), "parameter schema")
    kind = schema.get("type")
    if isinstance(kind, str):
        return kind
    alternatives = schema.get("anyOf", [])
    if isinstance(alternatives, list):
        for alternative in alternatives:
            candidate = object_value(alternative, "parameter alternative").get("type")
            if isinstance(candidate, str) and candidate != "null":
                return candidate
    return "string"


def validate_parameters(operation: Operation, values: JsonObject) -> JsonObject:
    known = {str(parameter["name"]): parameter for parameter in operation.parameters}
    unknown = values.keys() - known.keys()
    if unknown:
        raise CliError(f"Unknown parameters: {', '.join(sorted(unknown))}.")
    for name, parameter in known.items():
        if name not in values:
            if parameter.get("required"):
                raise CliError(f"Missing required parameter: {name}.")
            continue
        value = values[name]
        kind = parameter_type(parameter)
        valid = (
            (kind == "integer" and isinstance(value, int) and not isinstance(value, bool))
            or (kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool))
            or (kind == "boolean" and isinstance(value, bool))
            or (kind == "string" and isinstance(value, str))
        )
        if not valid:
            raise CliError(f"Parameter {name} must be {kind}.")
        definition = object_value(parameter.get("schema", {}), "parameter schema")
        for bound, compare in (("minimum", "minimum"), ("maximum", "maximum")):
            limit = definition.get(bound)
            if isinstance(value, (int, float)) and isinstance(limit, (int, float)):
                if (compare == "minimum" and value < limit) or (compare == "maximum" and value > limit):
                    raise CliError(f"Parameter {name} violates {bound} {limit}.")
        allowed = definition.get("enum")
        if isinstance(allowed, list) and value not in allowed:
            raise CliError(f"Invalid value for {name}.")
    return values
