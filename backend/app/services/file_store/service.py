"""Local-disk FileStore with AES-256-GCM encryption at rest."""

import asyncio
import base64
import binascii
import os
import re
import uuid
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import Settings
from app.services.file_store.contract import FileNotFound, FileStore, FileTampered
from app.shared.errors import PermanentError

_KEY_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_FORMAT_VERSION = b"\x01"
_NONCE_BYTES = 12


class DefaultFileStore:
    """File layout: version byte, 12-byte nonce, ciphertext with GCM tag.

    The storage key is the associated data, so a file copied under another key fails to
    decrypt. Files are sharded by the first two key characters to keep directories small.
    The content type is not stored here; the files table records it.
    """

    def __init__(self, *, root: Path, encryption_key: str) -> None:
        try:
            key = base64.b64decode(encryption_key, validate=True)
        except binascii.Error as exc:
            raise ValueError("FILE_ENCRYPTION_KEY must be base64") from exc
        if len(key) != 32:
            raise ValueError("FILE_ENCRYPTION_KEY must decode to 32 bytes")
        self._aead = AESGCM(key)
        self._root = root

    async def put(self, data: bytes, *, content_type: str) -> str:
        key = uuid.uuid4().hex
        nonce = os.urandom(_NONCE_BYTES)
        blob = _FORMAT_VERSION + nonce + self._aead.encrypt(nonce, data, key.encode())
        await asyncio.to_thread(self._write, self._path(key), blob)
        return key

    async def get(self, key: str) -> bytes:
        if not _KEY_PATTERN.match(key):
            raise FileNotFound(key)
        path = self._path(key)
        try:
            blob = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise FileNotFound(key) from exc
        if blob[:1] != _FORMAT_VERSION:
            raise FileTampered(key)
        nonce, ciphertext = blob[1 : 1 + _NONCE_BYTES], blob[1 + _NONCE_BYTES :]
        try:
            return self._aead.decrypt(nonce, ciphertext, key.encode())
        except InvalidTag as exc:
            raise FileTampered(key) from exc

    def _path(self, key: str) -> Path:
        return self._root / key[:2] / key

    @staticmethod
    def _write(path: Path, blob: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        tmp.replace(path)  # atomic: readers never see a partial file


class FileStoreNotConfigured(PermanentError):
    """FILE_ENCRYPTION_KEY is not set, so no file can be stored or read."""


class UnconfiguredFileStore:
    """Stands in when no encryption key is set: the app starts, and every file operation
    fails with a clear, permanent error."""

    async def put(self, data: bytes, *, content_type: str) -> str:
        raise FileStoreNotConfigured("FILE_ENCRYPTION_KEY is not set")

    async def get(self, key: str) -> bytes:
        raise FileStoreNotConfigured("FILE_ENCRYPTION_KEY is not set")


def build_file_store(settings: Settings) -> FileStore:
    if settings.file_encryption_key is None:
        return UnconfiguredFileStore()
    return DefaultFileStore(
        root=settings.file_store_dir,
        encryption_key=settings.file_encryption_key.get_secret_value(),
    )
