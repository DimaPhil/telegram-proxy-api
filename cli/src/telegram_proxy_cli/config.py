from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .errors import CliError


@dataclass(frozen=True)
class Credentials:
    base_url: str
    token: str


def normalize_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        _ = parsed.port
    except ValueError:
        raise CliError("Invalid API base URL.") from None
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 33 for char in value)
    ):
        raise CliError("API URL must be an HTTP(S) URL without credentials, query, or fragment.")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise CliError("Use HTTPS for remote APIs; HTTP is allowed only on loopback.")
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "tg" / "credentials.json"


def load_credentials() -> Credentials | None:
    path = config_path()
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise CliError("Saved credentials must be a regular private file (chmod 600).")
    try:
        data = json.loads(path.read_text())
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("base_url"), str)
            or not isinstance(data.get("token"), str)
        ):
            raise ValueError
        return Credentials(normalize_url(data["base_url"]), data["token"])
    except (ValueError, OSError):
        raise CliError("Cannot read saved credentials. Run tg login again.") from None


def save_credentials(credentials: Credentials) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump({"base_url": credentials.base_url, "token": credentials.token}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def resolve_credentials(base_url: str | None = None) -> Credentials:
    saved = load_credentials()
    requested = base_url or os.environ.get("TG_API_URL")
    url = normalize_url(requested or (saved.base_url if saved else "http://localhost:4040"))
    environment_token = os.environ.get("TG_API_TOKEN")
    # A URL override must never send credentials saved for a different service.
    if environment_token is not None:
        token = environment_token.strip()
    elif saved and saved.base_url == url:
        token = saved.token
    else:
        token = ""
        if saved and requested:
            raise CliError("This API URL differs from the saved login. Set TG_API_TOKEN or run tg login for that URL.")
    return Credentials(url, token)
