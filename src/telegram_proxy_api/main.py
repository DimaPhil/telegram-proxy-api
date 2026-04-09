from __future__ import annotations

import uvicorn

from .app import app
from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        app,
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
