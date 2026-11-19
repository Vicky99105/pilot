import uuid

from app.services.file_store.contract import FileNotFound


class FakeFileStore:
    """Implements the FileStore contract in memory."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, str]] = {}

    async def put(self, data: bytes, *, content_type: str) -> str:
        key = uuid.uuid4().hex
        self.files[key] = (data, content_type)
        return key

    async def get(self, key: str) -> bytes:
        if key not in self.files:
            raise FileNotFound(key)
        return self.files[key][0]
