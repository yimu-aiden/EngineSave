"""The object store (D18, 0.2.0): each file is streamed into ``objects/`` through a temporary
file, hashed while it is copied, and hard-linked (or copied) into the snapshot; every placed
file is hashed again. Tests replace ``hash_file`` and ``copy_and_verify`` *here*."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import secrets
import shutil
import zipfile
import zlib
from pathlib import Path
from typing import BinaryIO

from enginesave.fsutil import CHUNK_SIZE, describe_error, long_path, sha256_file
from enginesave.repo import Repo

log = logging.getLogger(__name__)

# Read-back verification hash; a module attribute so tests can inject failures.
hash_file = sha256_file


class SourceReadError(Exception):
    """The source file could not be read (recorded in ``errors``; snapshot becomes partial)."""


class VerifyMismatch(Exception):
    """The copy read back differently from what was read from the source."""


# ------------------------------------------------------------------------------- execution


def _ingest_stream(source: BinaryIO, tmp: Path, limit: int | None = None) -> tuple[str, int]:
    """Copy ``source`` into ``tmp`` while hashing; read errors raise SourceReadError.

    With ``limit``, reading stops one byte past it (callers compare sizes), which keeps a
    lying archive member from filling the disk.
    """
    digest = hashlib.sha256()
    size = 0
    with open(long_path(tmp), "xb") as out:
        while limit is None or size <= limit:
            try:
                chunk = source.read(CHUNK_SIZE)
            except (OSError, zipfile.BadZipFile, EOFError, zlib.error) as exc:
                raise SourceReadError(describe_error(exc)) from exc
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    return digest.hexdigest(), size


def _ingest(src: Path, tmp: Path) -> tuple[str, int]:
    """Stream ``src`` into ``tmp`` while hashing; source problems raise SourceReadError."""
    try:
        source = open(long_path(src), "rb")
    except OSError as exc:
        raise SourceReadError(describe_error(exc)) from exc
    with source:
        return _ingest_stream(source, tmp)


def _place(obj: Path, target: Path) -> str:
    """Hard-link ``target`` to the object; copy instead where links are unsupported or full.
    Never over a file already there: a copy over a hard link would write into the object."""
    try:
        os.link(long_path(obj), long_path(target))
        return "hardlink"
    except FileExistsError:
        raise
    except OSError:
        shutil.copyfile(long_path(obj), long_path(target))
        return "copy"


def _discard(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.unlink(long_path(path))


def copy_and_verify(repo: Repo, src: Path, target: Path) -> tuple[str, int, str]:
    """Store ``src`` in the object store and place it at ``target`` (D18).

    Returns ``(sha256, size, storage)``, storage being ``"hardlink"`` or ``"copy"``; the data
    file is hashed again once placed. Source problems raise :class:`SourceReadError`; a
    read-back difference raises :class:`VerifyMismatch` and leaves nothing at ``target``;
    destination errors propagate. Objects are never modified: when an existing object turns
    out to be damaged, the freshly read bytes are stored as a plain copy instead.
    """
    tmp = _object_tmp(repo, target)
    try:
        digest, size = _ingest(src, tmp)
        return digest, size, _store_and_place(repo, tmp, digest, size, target)
    finally:
        _discard(tmp)


def store_stream(
    repo: Repo, source: BinaryIO, target: Path, expected_sha256: str, expected_size: int
) -> str:
    """Like :func:`copy_and_verify` for a stream whose content is known in advance (import).

    The content must hash to ``expected_sha256`` with ``expected_size`` bytes, otherwise
    :class:`VerifyMismatch` is raised and nothing is stored. Returns the storage kind.
    """
    tmp = _object_tmp(repo, target)
    try:
        digest, size = _ingest_stream(source, tmp, limit=expected_size)
        if (digest, size) != (expected_sha256, expected_size):
            raise VerifyMismatch("content does not match the manifest")
        return _store_and_place(repo, tmp, digest, size, target)
    finally:
        _discard(tmp)


def _object_tmp(repo: Repo, target: Path) -> Path:
    tmp_dir = repo.object_tmp_dir()
    os.makedirs(long_path(tmp_dir), exist_ok=True)
    os.makedirs(long_path(target.parent), exist_ok=True)
    return tmp_dir / secrets.token_hex(8)


def _store_and_place(repo: Repo, tmp: Path, digest: str, size: int, target: Path) -> str:
    """Move ``tmp`` into the object store (or reuse the object) and place ``target``."""
    obj = repo.object_path(digest)
    created = False
    if not os.path.exists(long_path(obj)):
        os.makedirs(long_path(obj.parent), exist_ok=True)
        os.replace(long_path(tmp), long_path(obj))
        created = True
    if created or os.stat(long_path(obj)).st_size == size:
        storage = _place(obj, target)
        if hash_file(target) == (digest, size):
            return storage
        _discard(target)
        if created:
            raise VerifyMismatch("stored copy read back differently from the source")
    log.warning("object %s in %s is damaged; storing a plain copy", digest, repo.root)
    os.replace(long_path(tmp), long_path(target))
    if hash_file(target) != (digest, size):
        _discard(target)
        raise VerifyMismatch("stored copy read back differently from the source")
    return "copy"


def storage_kind(storages: set[str]) -> str:
    """Manifest ``storage``: ``hardlink``, ``copy`` or ``mixed`` (no files counts as hardlink)."""
    if len(storages) > 1:
        return "mixed"
    return next(iter(storages), "hardlink")
