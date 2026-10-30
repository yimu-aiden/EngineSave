"""Zip the portable folder and write its SHA-256 (tools/build_portable.ps1, plan M15).

usage: python tools/package_portable.py DIST_FOLDER ZIP_PATH

The archive holds the folder itself (``EngineSave/...``) so that it unpacks into one folder.
A ``config`` folder (settings and logs that a check of the build left behind) is never packed.
Beside the zip, ``<zip>.sha256`` gets ``<hex>  <file name>`` as ``sha256sum`` writes it.
"""

from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

LEFT_OUT = "config"


def pack(folder: Path, archive: Path) -> int:
    """Write ``archive`` (through a temporary file); returns the number of files packed."""
    tmp = archive.with_name(archive.name + ".tmp")
    count = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder)
            if relative.parts[0].casefold() == LEFT_OUT or not path.is_file():
                continue
            zf.write(path, f"{folder.name}/{relative.as_posix()}")
            count += 1
    tmp.replace(archive)
    return count


def write_sha256(archive: Path) -> Path:
    digest = hashlib.sha256()
    with archive.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    target = archive.with_name(archive.name + ".sha256")
    target.write_text(f"{digest.hexdigest()}  {archive.name}\n", encoding="ascii", newline="\n")
    return target


def main(argv: list[str]) -> int:
    folder, archive = Path(argv[0]), Path(argv[1])
    if (folder / LEFT_OUT).exists():
        print(f"note: {folder / LEFT_OUT} is left out of the archive", file=sys.stderr)
    count = pack(folder, archive)
    checksum = write_sha256(archive)
    print(f"{archive}: {count} files, {archive.stat().st_size} bytes")
    print(checksum.read_text(encoding="ascii").strip())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
