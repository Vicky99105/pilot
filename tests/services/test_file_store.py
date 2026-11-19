import base64
import os
from pathlib import Path

import pytest

from app.services.file_store.contract import FileNotFound, FileStore, FileTampered
from app.services.file_store.service import DefaultFileStore
from tests.fakes.file_store import FakeFileStore

KEY = base64.b64encode(os.urandom(32)).decode()


def _files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def _store(root: Path, key: str = KEY) -> FileStore:
    return DefaultFileStore(root=root, encryption_key=key)


async def test_round_trip_returns_original_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)

    key = await store.put(b"%PDF-1.7 claim form", content_type="application/pdf")

    assert await store.get(key) == b"%PDF-1.7 claim form"


async def test_bytes_on_disk_are_encrypted(tmp_path: Path) -> None:
    await _store(tmp_path).put(b"patient name: Asha Verma", content_type="text/plain")

    on_disk = b"".join(p.read_bytes() for p in _files(tmp_path))

    assert b"Asha Verma" not in on_disk


async def test_tampered_file_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    key = await store.put(b"bill total 45000", content_type="text/plain")
    [path] = _files(tmp_path)
    data = bytearray(path.read_bytes())
    data[-1] ^= 0x01
    path.write_bytes(bytes(data))

    with pytest.raises(FileTampered):
        await store.get(key)


async def test_file_swapped_under_another_key_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    key_a = await store.put(b"file a", content_type="text/plain")
    key_b = await store.put(b"file b", content_type="text/plain")
    paths = {p.name: p for p in _files(tmp_path)}
    paths[key_a].write_bytes(paths[key_b].read_bytes())

    with pytest.raises(FileTampered):
        await store.get(key_a)


async def test_wrong_key_cannot_decrypt(tmp_path: Path) -> None:
    key = await _store(tmp_path).put(b"secret", content_type="text/plain")
    other = _store(tmp_path, base64.b64encode(os.urandom(32)).decode())

    with pytest.raises(FileTampered):
        await other.get(key)


@pytest.mark.parametrize("bad_key", ["../../etc/passwd", "missing0000000000000000000000000"])
async def test_unknown_or_unsafe_key_raises_not_found(tmp_path: Path, bad_key: str) -> None:
    with pytest.raises(FileNotFound):
        await _store(tmp_path).get(bad_key)


def test_encryption_key_must_be_32_bytes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        DefaultFileStore(root=tmp_path, encryption_key=base64.b64encode(b"short").decode())


async def test_fake_file_store_round_trip() -> None:
    store: FileStore = FakeFileStore()

    key = await store.put(b"x", content_type="image/png")

    assert await store.get(key) == b"x"
