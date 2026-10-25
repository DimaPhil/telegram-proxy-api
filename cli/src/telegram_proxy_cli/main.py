from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Callable, Never, cast

from .catalog import COMMANDS, Json, JsonObject, Operation, object_value, operations, parameter_type
from .client import ApiClient
from .completion import candidates, install_completion, script
from .config import Credentials, config_path, normalize_url, resolve_credentials, save_credentials
from .errors import CliError
from .skill import install_skill


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise CliError(message)


def boolean(value: str) -> bool:
    if value.lower() not in {"true", "false"}:
        raise argparse.ArgumentTypeError("expected true or false")
    return value.lower() == "true"


def json_argument(value: str) -> Json:
    try:
        return cast(Json, json.loads(value))
    except ValueError:
        raise CliError("Invalid JSON argument.") from None


def build_parser(catalog: list[Operation]) -> Parser:
    parser = Parser(
        prog="tg",
        description="Telegram Proxy API client. JSON on stdout, errors on stderr. Read operations never retry automatically.",
    )
    parser.add_argument("--version", action="version", version=f"tg {version('telegram-proxy-cli')}")
    parser.add_argument("--api-url", help="Override API URL; saved tokens stay bound to their saved URL")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Validate and save API credentials")
    login.add_argument("--url", required=True)
    tokens = login.add_mutually_exclusive_group()
    tokens.add_argument("--token-stdin", action="store_true", help="Read token from stdin instead of terminal")
    tokens.add_argument("--anonymous", action="store_true", help="Connect to an API with authentication disabled")
    commands.add_parser("logout", help="Remove saved API credentials")
    status = commands.add_parser("auth-status", help="Report login configuration without revealing credentials")
    status.add_argument("--remote", action="store_true", help="Also verify API connectivity")
    skill = commands.add_parser("skill", help="Manage the bundled agent skill")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    install = skill_commands.add_parser("install")
    install.add_argument(
        "--if-safe", action="store_true", help="Warn instead of failing when an unmanaged skill conflicts"
    )
    completion = commands.add_parser("completion", help="Generate or install shell completion")
    completion_commands = completion.add_subparsers(dest="completion_command", required=True)
    for shell in ("bash", "zsh", "fish"):
        completion_commands.add_parser(shell)
    completion_install = completion_commands.add_parser("install")
    completion_install.add_argument("--if-safe", action="store_true")
    completion_install.add_argument(
        "--no-shell-init", action="store_true", help="Install files without editing shell startup files"
    )
    complete = completion_commands.add_parser("candidates", help=argparse.SUPPRESS)
    complete.add_argument("words", nargs=argparse.REMAINDER)
    discovery = commands.add_parser("operations", help="Discover or call the exact bundled API contract")
    discovery_commands = discovery.add_subparsers(dest="operation_command", required=True)
    discovery_commands.add_parser("list")
    describe = discovery_commands.add_parser("describe")
    describe.add_argument("operation_id")
    call = discovery_commands.add_parser("call")
    call.add_argument("operation_id")
    call.add_argument("--params", default="{}", help="JSON object containing path and query parameters")
    call.add_argument("--body", help="JSON request body, when the operation supports one")
    add_download_flags(call)
    groups: dict[str, argparse._SubParsersAction[Parser]] = {}
    for operation in catalog:
        command = COMMANDS.get((operation.method, operation.path))
        if command is None:
            continue  # Universal operations call remains available; parity CI requires explicit review.
        if len(command) == 1:
            leaf = commands.add_parser(command[0], help=str(operation.document.get("summary", operation.id)))
        else:
            if command[0] not in groups:
                group = commands.add_parser(command[0])
                groups[command[0]] = group.add_subparsers(dest="resource_command", required=True)
            leaf = groups[command[0]].add_parser(command[1], help=str(operation.document.get("summary", operation.id)))
        leaf.set_defaults(operation_id=operation.id)
        for parameter in operation.parameters:
            name = str(parameter["name"])
            converters: dict[str, Callable[[str], str | int | float | bool]] = {
                "integer": int,
                "number": float,
                "boolean": boolean,
                "string": str,
            }
            converter = converters.get(parameter_type(parameter), str)
            leaf.add_argument(
                "--" + name.replace("_", "-"),
                dest="parameter_" + name,
                type=converter,
                required=bool(parameter.get("required")),
                default=argparse.SUPPRESS,
                help=str(parameter.get("description", name)),
            )
        if "requestBody" in operation.document:
            leaf.add_argument("--body")
        add_download_flags(leaf)
    return parser


def add_download_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", type=Path, help="Save response to file (required for binary downloads)")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing the chosen output file")


def emit(value: object, *, error: bool = False) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), file=sys.stderr if error else sys.stdout)


def execute(args: argparse.Namespace, catalog: list[Operation]) -> None:
    if args.command == "completion":
        if args.completion_command == "install":
            try:
                emit(install_completion(shell_init=not args.no_shell_init))
            except CliError as error:
                if not args.if_safe:
                    raise
                emit({"warning": str(error), "installed": False}, error=True)
        elif args.completion_command == "candidates":
            words = cast(list[str], args.words)
            if words and words[0] == "--":
                words = words[1:]
            for value in candidates(build_parser(catalog), words, [operation.id for operation in catalog]):
                print(value)
        else:
            print(script(cast(str, args.completion_command)), end="")
        return
    if args.command == "login":
        url = normalize_url(cast(str, args.url))
        if args.anonymous:
            token = ""
        elif args.token_stdin:
            token = sys.stdin.read().strip()
        else:
            token = os.environ.get("TG_API_TOKEN", "").strip()
            if not token:
                if not sys.stdin.isatty():
                    raise CliError("Use --token-stdin or TG_API_TOKEN in noninteractive sessions.")
                token = getpass.getpass("API bearer token: ").strip()
        if not token and not args.anonymous:
            raise CliError("An API bearer token is required (or explicitly use --anonymous).")
        credentials = Credentials(url, token)
        with ApiClient(credentials) as client:
            client.check_login()
        save_credentials(credentials)
        emit({"authenticated": bool(token), "base_url": url, "saved": True})
        return
    if args.command == "logout":
        config_path().unlink(missing_ok=True)
        emit({"logged_out": True})
        return
    if args.command == "auth-status":
        credentials = resolve_credentials(cast(str | None, args.api_url))
        if args.remote:
            with ApiClient(credentials) as client:
                client.check_login()
        emit(
            {
                "base_url": credentials.base_url,
                "has_token": bool(credentials.token),
                "remote_checked": bool(args.remote),
            }
        )
        return
    if args.command == "skill":
        try:
            emit(install_skill())
        except CliError as error:
            if not args.if_safe:
                raise
            emit({"warning": str(error), "installed": False}, error=True)
        return
    if args.command == "operations" and args.operation_command == "list":
        emit(
            [
                {
                    "id": operation.id,
                    "method": operation.method,
                    "path": operation.path,
                    "command": list(COMMANDS.get((operation.method, operation.path), ())),
                }
                for operation in catalog
            ]
        )
        return
    operation_id = cast(str, args.operation_id)
    operation = next((candidate for candidate in catalog if candidate.id == operation_id), None)
    if operation is None:
        raise CliError("Unknown operation. Run tg operations list.")
    if args.command == "operations" and args.operation_command == "describe":
        emit({"id": operation.id, "method": operation.method, "path": operation.path, "definition": operation.document})
        return
    if args.command == "operations":
        parameters = object_value(json_argument(cast(str, args.params)), "--params")
    else:
        parameters = cast(
            JsonObject,
            {
                key.removeprefix("parameter_"): value
                for key, value in vars(args).items()
                if key.startswith("parameter_")
            },
        )
    body_argument = cast(str | None, getattr(args, "body", None))
    body = json_argument(body_argument) if body_argument is not None else None
    with ApiClient(resolve_credentials(cast(str | None, args.api_url))) as client:
        emit(
            client.call(
                operation, parameters, body=body, output=cast(Path | None, args.output), overwrite=bool(args.overwrite)
            )
        )


def main(argv: list[str] | None = None) -> int:
    try:
        catalog = operations()
        execute(build_parser(catalog).parse_args(argv), catalog)
        return 0
    except CliError as error:
        emit(error.payload(), error=True)
        return 1
    except (OSError, UnicodeError):
        emit({"error": "Local file operation failed. Check permissions and paths."}, error=True)
        return 1
    except KeyboardInterrupt:
        emit({"error": "Interrupted."}, error=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
