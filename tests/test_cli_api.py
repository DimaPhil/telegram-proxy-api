"""Exercise the independently packaged client against the real FastAPI routes."""

from __future__ import annotations

import httpx
import pytest
from telegram_proxy_cli.catalog import operations
from telegram_proxy_cli.client import ApiClient
from telegram_proxy_cli.config import Credentials
from telegram_proxy_cli.errors import CliError

from tests.test_app import create_test_client


def test_cli_calls_every_api_operation(tmp_path):
    with create_test_client(auth_enabled=True, bearer_token="test-token") as api:

        def request(request: httpx.Request) -> httpx.Response:
            response = api.request(
                request.method,
                request.url.raw_path.decode(),
                headers=dict(request.headers),
                content=request.content,
            )
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)

        with ApiClient(
            Credentials("https://api.example", "test-token"), transport=httpx.MockTransport(request)
        ) as client:
            client.check_login()
            for operation in operations():
                values = {
                    "chat_id": "100",
                    "message_id": 10,
                    "media_id": "10:0",
                    "value": "@resolved",
                    "query": "meeting & plans",
                }
                params = {
                    str(parameter["name"]): values[str(parameter["name"])]
                    for parameter in operation.parameters
                    if parameter.get("required")
                }
                output = tmp_path / operation.id if operation.binary else None
                result = client.call(operation, params, output=output)
                if output:
                    assert output.read_bytes() == (b"zip" if operation.id == "media_bundle" else b"abc")
                    assert result["provider"] == "telethon"
                else:
                    assert "data" in result
                    if operation.id != "health":
                        assert result["meta"]["provider"] == "telethon"

        with ApiClient(Credentials("https://api.example", "wrong"), transport=httpx.MockTransport(request)) as client:
            with pytest.raises(CliError) as caught:
                client.check_login()
            assert caught.value.status == 401
