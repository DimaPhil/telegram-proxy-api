from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import cast
from urllib.parse import quote

import httpx

from .catalog import Json, JsonObject, Operation, validate_parameters
from .config import Credentials
from .errors import CliError


class ApiClient:
    def __init__(
        self, credentials: Credentials, *, transport: httpx.BaseTransport | None = None, timeout: float = 30
    ) -> None:
        self.credentials = credentials
        self.http = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
            headers={"Authorization": f"Bearer {credentials.token}"} if credentials.token else {},
        )

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.http.close()

    def _check_response(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        if response.is_redirect:
            raise CliError(
                "API redirected the request; redirects are disabled to protect credentials.",
                status=response.status_code,
            )
        detail = f"API request failed (HTTP {response.status_code})."
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("detail"), str):
                detail = body["detail"][:2000]
        except (ValueError, httpx.ResponseNotRead):
            pass
        if self.credentials.token:
            detail = detail.replace(self.credentials.token, "[redacted]")
        raise CliError(detail, status=response.status_code, retry_after=response.headers.get("Retry-After"))

    def check_login(self) -> None:
        try:
            response = self.http.get(f"{self.credentials.base_url}/healthz")
            self._check_response(response)
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("data"), dict) or "status" not in body["data"]:
                raise CliError("The URL did not return Telegram Proxy API health data.")
        except httpx.HTTPError:
            raise CliError("Cannot connect to API. Check the URL, TLS, and network.") from None
        except ValueError:
            raise CliError("The API returned invalid JSON.") from None

    def call(
        self,
        operation: Operation,
        parameters: JsonObject,
        *,
        body: Json = None,
        output: Path | None = None,
        overwrite: bool = False,
    ) -> Json:
        validate_parameters(operation, parameters)
        path = operation.path
        query: dict[str, str] = {}
        for parameter in operation.parameters:
            name = str(parameter["name"])
            if name not in parameters:
                continue
            value = parameters[name]
            text = str(value).lower() if isinstance(value, bool) else str(value)
            if parameter.get("in") == "path":
                # Dot segments can be normalized by HTTP clients even when URI encoded.
                if text in {".", ".."}:
                    raise CliError(f"Invalid path parameter: {name}.")
                path = path.replace("{" + name + "}", quote(text, safe=""))
            elif parameter.get("in") == "query":
                query[name] = text
            else:
                raise CliError(f"Unsupported parameter location for {name}; update the CLI contract.")
        if output is None and operation.binary:
            raise CliError("A binary operation requires --output FILE.")
        if body is not None and "requestBody" not in operation.document:
            raise CliError("This operation does not accept a JSON body.")
        request_body = operation.document.get("requestBody")
        if isinstance(request_body, dict) and request_body.get("required") and body is None:
            raise CliError("This operation requires --body JSON.")
        try:
            with self.http.stream(
                operation.method, self.credentials.base_url + path, params=query, json=body
            ) as response:
                if not response.is_success:
                    response.read()
                    self._check_response(response)
                if output is not None:
                    self._download(response, output, overwrite=overwrite)
                    return {"output": str(output), "provider": response.headers.get("X-Telegram-Provider")}
                response.read()
                return cast(Json, response.json())
        except httpx.HTTPError:
            raise CliError("API transport failed. No automatic retry was performed.") from None
        except json.JSONDecodeError:
            raise CliError("API returned invalid JSON.") from None

    @staticmethod
    def _download(response: httpx.Response, output: Path, *, overwrite: bool) -> None:
        if not overwrite and (output.exists() or output.is_symlink()):
            raise CliError("Output exists; choose another path or use --overwrite.")
        fd, temporary_name = tempfile.mkstemp(prefix=".tg-download-", dir=output.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as destination:
                for chunk in response.iter_bytes():
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            if overwrite:
                os.replace(temporary, output)
            else:
                # Atomic no-clobber publication even if a file appears during download.
                os.link(temporary, output)
        except FileExistsError:
            raise CliError("Output appeared during download; it was not overwritten.") from None
        finally:
            temporary.unlink(missing_ok=True)
