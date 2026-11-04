from __future__ import annotations


class CliError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    def payload(self) -> dict[str, str | int]:
        payload: dict[str, str | int] = {"error": str(self)}
        if self.status is not None:
            payload["status"] = self.status
        if self.retry_after is not None:
            payload["retry_after"] = self.retry_after
        return payload
