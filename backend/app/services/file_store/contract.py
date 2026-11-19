"""Contract for encrypted file storage. Callers import this module only."""

from typing import Protocol


class FileNotFound(Exception):
    """No file exists under the given storage key."""


class FileTampered(Exception):
    """The stored bytes failed authentication: changed, swapped or wrong key."""


class FileStore(Protocol):
    async def put(self, data: bytes, *, content_type: str) -> str: ...  # returns storage key

    async def get(self, key: str) -> bytes: ...
