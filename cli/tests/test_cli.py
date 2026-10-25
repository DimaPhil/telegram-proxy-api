from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from telegram_proxy_cli.catalog import COMMANDS, Operation, operations, validate_parameters
from telegram_proxy_cli.client import ApiClient
from telegram_proxy_cli.config import (
    Credentials,
    config_path,
    load_credentials,
    normalize_url,
    resolve_credentials,
    save_credentials,
)
from telegram_proxy_cli.errors import CliError
from telegram_proxy_cli.main import build_parser, main
from telegram_proxy_cli.skill import install_skill


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("TG_API_TOKEN", raising=False)
    monkeypatch.delenv("TG_API_URL", raising=False)


def operation(identifier: str) -> Operation:
    return next(item for item in operations() if item.id == identifier)


def test_contract_and_every_friendly_command() -> None:
    catalog = operations()
    assert {(item.method, item.path) for item in catalog} == set(COMMANDS)
    parser = build_parser(catalog)
    for item in catalog:
        arguments = list(COMMANDS[item.method, item.path])
        for parameter in item.parameters:
            if parameter.get("required"):
                arguments.extend(["--" + str(parameter["name"]).replace("_", "-"), "1"])
        parsed = parser.parse_args(arguments)
        assert parsed.operation_id == item.id


def test_http_request_preserves_identity_and_envelope() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert request.url.path == "/proxy/chats/-100123/messages/42"
        return httpx.Response(200, json={"data": {"id": "42"}, "meta": {"provider": "bot_cache"}})

    with ApiClient(
        Credentials("https://api.example/proxy", "test-secret"), transport=httpx.MockTransport(handle)
    ) as client:
        result = client.call(operation("messages_get"), {"chat_id": "-100123", "message_id": 42})
    assert result == {"data": {"id": "42"}, "meta": {"provider": "bot_cache"}}


def test_query_boolean_and_path_encoding() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert "include_album=false" in str(request.url)
        assert b"a%2Fb" in request.url.raw_path
        return httpx.Response(200, json={"data": []})

    with ApiClient(Credentials("https://api.example", ""), transport=httpx.MockTransport(handle)) as client:
        client.call(operation("media_list"), {"chat_id": "a/b", "message_id": 1, "include_album": False})


@pytest.mark.parametrize("status", [302, 401, 429, 503])
def test_errors_do_not_follow_redirects_or_retry_or_reveal_token(status: int) -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            headers={"Location": "https://evil.example", "Retry-After": "17"},
            json={"detail": "failure test-secret"},
        )

    with ApiClient(Credentials("https://api.example", "test-secret"), transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(CliError) as caught:
            client.call(operation("me"), {})
    assert calls == 1
    assert caught.value.status == status
    assert "test-secret" not in str(caught.value)
    if status != 302:
        assert caught.value.retry_after == "17"


def test_transport_error_is_sanitized() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret-transport-details", request=request)

    with ApiClient(Credentials("https://api.example", ""), transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(CliError, match="transport failed") as caught:
            client.call(operation("me"), {})
    assert "secret-transport-details" not in str(caught.value)


def test_download_is_atomic_and_no_clobber(tmp_path: Path) -> None:
    output = tmp_path / "media.bin"
    with ApiClient(
        Credentials("https://api.example", ""),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"media")),
    ) as client:
        params = {"chat_id": "1", "message_id": 2, "media_id": "photo"}
        with pytest.raises(CliError, match="requires --output"):
            client.call(operation("media_download"), params)
        client.call(operation("media_download"), params, output=output)
        assert output.read_bytes() == b"media"
        with pytest.raises(CliError, match="Output exists"):
            client.call(operation("media_download"), params, output=output)
        client.call(operation("media_download"), params, output=output, overwrite=True)
    assert list(tmp_path.iterdir()) == [output]


def test_interrupted_download_leaves_existing_file(tmp_path: Path) -> None:
    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial"
            raise httpx.ReadError("broken")

    output = tmp_path / "media.bin"
    output.write_bytes(b"original")
    with ApiClient(
        Credentials("https://api.example", ""),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=BrokenStream())),
    ) as client:
        with pytest.raises(CliError):
            client.call(operation("media_bundle"), {"chat_id": "1", "message_id": 2}, output=output, overwrite=True)
    assert output.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://token@example.com",
        "https://example.com/?x=1",
        "https://example.com/#x",
        "https://example.com:bad",
        "file:///tmp/api",
        "https://example.com\n",
    ],
)
def test_unsafe_urls_rejected(url: str) -> None:
    with pytest.raises(CliError):
        normalize_url(url)


def test_saved_credentials_are_private_and_origin_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = Credentials("https://api.example", "test-secret")
    save_credentials(credentials)
    assert config_path().stat().st_mode & 0o777 == 0o600
    assert load_credentials() == credentials
    assert resolve_credentials() == credentials
    with pytest.raises(CliError, match="differs"):
        resolve_credentials("https://other.example")
    monkeypatch.setenv("TG_API_TOKEN", "other-token")
    assert resolve_credentials("https://other.example").token == "other-token"
    config_path().chmod(0o644)
    with pytest.raises(CliError, match="private"):
        load_credentials()


def test_login_validates_before_saving_and_never_prints_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TG_API_TOKEN", "test-secret")
    with patch.object(ApiClient, "check_login", side_effect=CliError("not accepted")):
        assert main(["login", "--url", "https://api.example"]) == 1
    assert not config_path().exists()
    with patch.object(ApiClient, "check_login"):
        assert main(["login", "--url", "https://api.example"]) == 0
    assert load_credentials() == Credentials("https://api.example", "test-secret")
    assert main(["auth-status"]) == 0
    captured = capsys.readouterr()
    assert "test-secret" not in captured.out + captured.err
    assert main(["logout"]) == 0
    assert not config_path().exists()


def test_parameter_validation_and_json_errors(capsys: pytest.CaptureFixture[str]) -> None:
    item = operation("messages_get")
    for values in (
        {"chat_id": "1"},
        {"chat_id": "1", "message_id": "42"},
        {"chat_id": "1", "message_id": 42, "extra": True},
    ):
        with pytest.raises(CliError):
            validate_parameters(item, values)
    assert main(["operations", "call", "messages_get", "--params", "bad"]) == 1
    assert "error" in json.loads(capsys.readouterr().err)
    assert main(["messages", "get"]) == 1
    assert "error" in json.loads(capsys.readouterr().err)


def test_operations_discovery_is_offline(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["operations", "list"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == len(COMMANDS)
    assert main(["operations", "describe", "messages_get"]) == 0
    assert json.loads(capsys.readouterr().out)["path"].endswith("/{message_id}")


def test_skill_install_update_and_conflict(tmp_path: Path) -> None:
    result = install_skill(home=tmp_path)
    target = Path(result["skill"])
    adapter = Path(result["claude_adapter"])
    assert adapter.is_symlink()
    assert os.readlink(adapter) == "../../.agents/skills/tg"
    assert (target / "SKILL.md").is_file()
    (target / "SKILL.md").write_text("old version")
    install_skill(home=tmp_path)
    assert "old version" not in (target / "SKILL.md").read_text()
    (target / "source.json").unlink()
    with pytest.raises(CliError, match="Unmanaged skill"):
        install_skill(home=tmp_path)


def test_skill_adapter_conflict_preserves_both(tmp_path: Path) -> None:
    adapter = tmp_path / ".claude" / "skills" / "tg"
    adapter.parent.mkdir(parents=True)
    adapter.write_text("unrelated")
    with pytest.raises(CliError, match="Unmanaged Claude"):
        install_skill(home=tmp_path)
    assert adapter.read_text() == "unrelated"
    assert not (tmp_path / ".agents" / "skills" / "tg").exists()


def test_skill_documented_examples_parse() -> None:
    import shlex
    from importlib.resources import files

    skill = files("telegram_proxy_cli").joinpath("skills", "tg", "SKILL.md").read_text()
    parser = build_parser(operations())
    examples = [line for line in skill.splitlines() if line.startswith("tg ")]
    assert len(examples) >= 8
    for example in examples:
        parser.parse_args(shlex.split(example)[1:])


def test_new_download_is_detected_from_schema() -> None:
    item = Operation("future_download", "GET", "/exports/{id}", operation("media_download").document, ())
    assert item.binary
    assert not operation("me").binary


@pytest.mark.parametrize("location,kind", [("header", "string"), ("query", "array"), ("cookie", "integer")])
def test_unsupported_parameter_changes_fail_contract(location: str, kind: str) -> None:
    schema = {
        "paths": {
            "/future": {
                "get": {
                    "operationId": "future",
                    "parameters": [{"name": "value", "in": location, "schema": {"type": kind}}],
                }
            }
        }
    }
    with pytest.raises(CliError, match="Unsupported parameter"):
        operations(schema)


def test_duplicate_operation_ids_fail_contract() -> None:
    with pytest.raises(CliError, match="unique"):
        operations({"paths": {"/one": {"get": {"operationId": "same"}}, "/two": {"get": {"operationId": "same"}}}})


def test_new_json_body_has_friendly_and_generic_support() -> None:
    item = operation("me")
    document = dict(item.document)
    document["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}}
    changed = Operation(item.id, item.method, item.path, document, item.parameters)
    parsed = build_parser([changed]).parse_args(["me", "--body", '{"value":1}'])
    assert parsed.body == '{"value":1}'
    with ApiClient(
        Credentials("https://api.example", ""),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=json.loads(request.content))),
    ) as client:
        with pytest.raises(CliError, match="requires --body"):
            client.call(changed, {})
        assert client.call(changed, {}, body={"value": 1}) == {"value": 1}


def test_non_json_request_body_fails_contract() -> None:
    with pytest.raises(CliError, match="Only JSON"):
        operations(
            {
                "paths": {
                    "/upload": {
                        "post": {"operationId": "upload", "requestBody": {"content": {"multipart/form-data": {}}}}
                    }
                }
            }
        )
