"""OpenAPI generation from lenApi routes and Pydantic models."""

import inspect
from html import escape
from typing import Any

from pydantic import BaseModel, TypeAdapter

from .background import BackgroundTasks
from .dependencies import Depends
from .request import Request, endpoint_type_hints, upload_file_shape
from .parameters import Body, File
from .security import APIKeyAuth, BasicAuth, HMACAuth, JWTAuth, OIDCAuth, OpaqueBearerAuth, SessionAuth, WebhookAuth


def _add_definitions(schema: dict[str, Any], components: dict[str, Any]) -> dict[str, Any]:
    for name, definition in schema.pop("$defs", {}).items():
        components.setdefault(name, definition)
    return schema


def _model_ref(model: type[BaseModel], components: dict[str, Any]) -> dict[str, str]:
    if model.__name__ not in components:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        components[model.__name__] = _add_definitions(schema, components)
    return {"$ref": f"#/components/schemas/{model.__name__}"}


def _type_schema(annotation: Any, components: dict[str, Any]) -> dict[str, Any]:
    schema = TypeAdapter(annotation).json_schema(ref_template="#/components/schemas/{model}")
    return _add_definitions(schema, components)


def _auth_schemes(dependency, seen=None) -> dict[str, dict[str, str]]:
    seen = set() if seen is None else seen
    if dependency in seen:
        return {}
    seen.add(dependency)
    if isinstance(dependency, JWTAuth):
        return {"BearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}}
    if isinstance(dependency, BasicAuth):
        return {"BasicAuth": {"type": "http", "scheme": "basic"}}
    if isinstance(dependency, APIKeyAuth):
        name = "ApiKey_" + "".join(char if char.isalnum() else "_" for char in dependency.header_name)
        return {name: {"type": "apiKey", "in": "header", "name": dependency.header_name}}
    if isinstance(dependency, SessionAuth):
        return {"SessionCookie": {"type": "apiKey", "in": "cookie", "name": dependency.cookie_name}}
    if isinstance(dependency, HMACAuth):
        return {
            "HMACTimestamp": {"type": "apiKey", "in": "header", "name": "X-LenAPI-Timestamp"},
            "HMACNonce": {"type": "apiKey", "in": "header", "name": "X-LenAPI-Nonce"},
            "HMACSignature": {"type": "apiKey", "in": "header", "name": dependency.header_name},
        }
    if isinstance(dependency, WebhookAuth):
        schemes = {
            "WebhookSignature": {
                "type": "apiKey", "in": "header", "name": dependency.signature_header
            }
        }
        if dependency.timestamp_header:
            schemes["WebhookTimestamp"] = {
                "type": "apiKey", "in": "header", "name": dependency.timestamp_header
            }
        if dependency.event_id_header and dependency.security_store is not None:
            schemes["WebhookEventId"] = {
                "type": "apiKey", "in": "header", "name": dependency.event_id_header
            }
        return schemes
    if isinstance(dependency, (OpaqueBearerAuth, OIDCAuth)):
        return {"BearerToken": {"type": "http", "scheme": "bearer"}}
    try:
        parameters = inspect.signature(dependency).parameters.values()
    except (TypeError, ValueError):
        return {}
    schemes = {}
    for parameter in parameters:
        if isinstance(parameter.default, Depends):
            schemes.update(_auth_schemes(parameter.default.dependency, seen))
    return schemes


def build_openapi(app, *, title: str, version: str) -> dict[str, Any]:
    components: dict[str, Any] = {}
    paths: dict[str, Any] = {}
    security_schemes: dict[str, dict[str, str]] = {}

    for route in app.routes:
        if not route.include_in_schema:
            continue
        for method in route.methods:
            operation: dict[str, Any] = {
                "operationId": f"{route.endpoint.__name__}_{method.lower()}",
                "summary": route.endpoint.__name__.replace("_", " ").capitalize(),
                "responses": {"200": {"description": "Successful response"}},
            }
            if route.tag_name is not None:
                operation["tags"] = [route.tag_name]
            if route.auth is not None:
                route_auth = route.auth.dependency if isinstance(route.auth, Depends) else route.auth
                schemes = _auth_schemes(route_auth)
                if schemes:
                    security_schemes.update(schemes)
                    operation["security"] = [{name: [] for name in schemes}]
            if route.request_body is not None:
                operation["requestBody"] = route.request_body
            if route.api_flag:
                operation["x-lenapi-throttle"] = {
                    "key": "client_ip",
                    "cool_time_seconds": route.cool_time,
                }
            if route.throttle:
                operation["x-lenapi-rate-limits"] = [
                    {
                        "scope": limit.scope,
                        "requests": limit.count,
                        "window_seconds": limit.window_seconds,
                    }
                    for limit in route.throttle
                ]
            parameters = []
            request_body = operation.get("requestBody")
            file_properties: dict[str, Any] = {}
            required_files: list[str] = []
            type_hints = endpoint_type_hints(route.endpoint)
            for name, parameter in inspect.signature(route.endpoint).parameters.items():
                annotation = type_hints.get(name, parameter.annotation)
                file_shape = upload_file_shape(annotation)
                if file_shape is not None:
                    field_schema: dict[str, Any] = {"type": "string", "format": "binary"}
                    if file_shape == "multiple":
                        field_schema = {"type": "array", "items": field_schema}
                    file_properties[name] = field_schema
                    marker = parameter.default if isinstance(parameter.default, File) else None
                    marker_default = marker.default if marker is not None else parameter.default
                    if marker_default is inspect.Parameter.empty or marker_default is ...:
                        required_files.append(name)
                    continue
                if isinstance(parameter.default, Body):
                    request_body = {
                        "required": True,
                        "content": {"application/json": {"schema": {}}},
                    }
                    continue
                if name == "request" and annotation in (Request, inspect.Parameter.empty):
                    continue
                if annotation is BackgroundTasks:
                    continue
                if isinstance(parameter.default, Depends):
                    schemes = _auth_schemes(parameter.default.dependency)
                    if schemes:
                        security_schemes.update(schemes)
                        existing = operation.get("security", [{}])[0]
                        existing.update({name: [] for name in schemes})
                        operation["security"] = [existing]
                    continue
                if getattr(annotation, "__module__", "").startswith("sqlalchemy"):
                    continue
                if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
                    request_body = {
                        "required": parameter.default is inspect.Parameter.empty,
                        "content": {"application/json": {"schema": _model_ref(annotation, components)}},
                    }
                    continue
                if annotation is inspect.Parameter.empty:
                    schema = {"type": "string"}
                else:
                    try:
                        schema = _type_schema(annotation, components)
                    except Exception:
                        schema = {"type": "string"}
                in_path = name in route.parameter_names
                parameters.append({
                    "name": name,
                    "in": "path" if in_path else "query",
                    "required": in_path or parameter.default is inspect.Parameter.empty,
                    "schema": schema,
                })
            if parameters:
                operation["parameters"] = parameters
            if file_properties and route.request_body is None:
                request_body = {
                    "required": bool(required_files),
                    "content": {
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "properties": file_properties,
                                **({"required": required_files} if required_files else {}),
                            }
                        }
                    },
                }
            if request_body is not None:
                operation["requestBody"] = request_body
            if route.response_model is not None:
                operation["responses"]["200"]["content"] = {
                    "application/json": {"schema": _type_schema(route.response_model, components)}
                }
            paths.setdefault(route.path, {})[method.lower()] = operation

    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version},
        "paths": paths,
        "components": {
            "schemas": components,
            **({"securitySchemes": security_schemes} if security_schemes else {}),
        },
    }


def swagger_html(openapi_url: str) -> str:
    return f'''<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>API docs</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
      window.onload = () => SwaggerUIBundle({{ url: {openapi_url!r}, dom_id: '#swagger-ui' }});
    </script>
  </body>
</html>'''


def redoc_html(openapi_url: str) -> str:
    """Return a small ReDoc page that loads the app's OpenAPI document."""
    escaped_url = escape(openapi_url, quote=True)
    return f'''<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>API reference</title>
  </head>
  <body>
    <redoc spec-url="{escaped_url}"></redoc>
    <script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"></script>
  </body>
</html>'''
