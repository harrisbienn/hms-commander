"""Bounded, staged intake of nested M3 project archives.

The caller owns the output root. Cooperating writers use an exclusive lock;
stale locks and retained attempts require explicit operator inspection.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unicodedata
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path, PureWindowsPath
from typing import BinaryIO, Iterator

import requests

from .LoggingConfig import get_logger

logger = get_logger(__name__)


def _parts(name: str, max_depth: int = 32) -> tuple[str, ...]:
    if not name or "\\" in name or "\0" in name or name.startswith("/"):
        raise ValueError(f"Unsafe archive path: {name!r}")
    parts = tuple((name[:-1] if name.endswith("/") else name).split("/"))
    if len(parts) > max_depth:
        raise ValueError("Archive path exceeds max_path_depth")
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or part.rstrip(" .") != part
            or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
            or PureWindowsPath(part).is_reserved()
        ):
            raise ValueError(f"Unsafe archive path: {name!r}")
    return parts


def _key(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", part).casefold() for part in parts)


def _linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _entries(
    archive: zipfile.ZipFile, max_members: int, max_path_depth: int
) -> list[tuple[zipfile.ZipInfo, tuple[str, ...]]]:
    members = archive.infolist()
    if len(members) > max_members:
        raise ValueError("Archive exceeds max_members")
    result, names, prefixes = [], set(), {}
    for member in members:
        parts = _parts(member.orig_filename, max_path_depth)
        key = _key(parts)
        if key in names:
            raise ValueError(f"Colliding archive path: {member.filename!r}")
        names.add(key)
        for depth in range(1, len(parts) + 1):
            prefix, original = key[:depth], parts[:depth]
            if prefix in prefixes and prefixes[prefix] != original:
                raise ValueError(f"Aliased archive path: {member.filename!r}")
            prefixes[prefix] = original
        kind = stat.S_IFMT(member.external_attr >> 16)
        if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or member.external_attr & 0x400:
            raise ValueError(
                f"Archive links or special entries are unsupported: {member.filename!r}"
            )
        if member.flag_bits & 1:
            raise ValueError("Encrypted archive entries are unsupported")
        result.append((member, parts))
    files = {_key(parts) for member, parts in result if not member.is_dir()}
    for _, parts in result:
        if any(_key(parts[:depth]) in files for depth in range(1, len(parts))):
            raise ValueError(f"Archive file/directory collision: {'/'.join(parts)!r}")
    return result


def _copy(source: BinaryIO, destination: BinaryIO, limit: int) -> int:
    count = 0
    while True:
        block = source.read(min(1024 * 1024, limit - count + 1))
        if not block:
            return count
        count += len(block)
        if count > limit:
            raise ValueError("Archive content exceeds its declared byte budget")
        if destination.write(block) != len(block):
            raise OSError("Incomplete staged archive write")


def _download(url: str, destination: Path, limit: int) -> None:
    with closing(requests.get(url, stream=True, timeout=300)) as response:
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared is not None and (int(declared) < 0 or int(declared) > limit):
            raise ValueError("Download exceeds max_archive_bytes")
        count = 0
        with destination.open("xb") as stream:
            for block in response.iter_content(chunk_size=64 * 1024):
                count += len(block)
                if count > limit:
                    raise ValueError("Download exceeds max_archive_bytes")
                if stream.write(block) != len(block):
                    raise OSError("Incomplete staged download")
            stream.flush()
            os.fsync(stream.fileno())


def _extract(
    outer_path: Path,
    attempt: Path,
    model_id: str,
    unit_id: str,
    max_archive_bytes: int,
    max_uncompressed_bytes: int,
    max_members: int,
    max_path_depth: int,
) -> Path:
    inner_path = attempt / "project.zip"
    patterns = {
        f"HEC-HMS/{prefix}{unit}.zip".casefold()
        for prefix in (f"{model_id}_", "")
        for unit in (unit_id, unit_id.replace("-", "_"))
    }
    with zipfile.ZipFile(outer_path) as outer:
        entries = _entries(outer, max_members, max_path_depth)
        candidates = [
            member
            for member, parts in entries
            if not member.is_dir()
            and len(parts) == 2
            and parts[0].casefold() == "hec-hms"
            and parts[-1].lower().endswith(".zip")
        ]
        matches = [
            member for member in candidates if member.filename.casefold() in patterns
        ]
        if not matches:
            unit_base = unit_id.replace("-", "").replace("_", "").casefold()
            matches = [
                member
                for member in candidates
                if unit_base
                in Path(member.filename)
                .stem.replace("_", "")
                .replace("-", "")
                .casefold()
            ]
        if not matches:
            raise FileNotFoundError(f"No nested HMS archive for unit {unit_id!r}")
        if len(matches) != 1:
            raise ValueError(f"Ambiguous nested HMS archive for unit {unit_id!r}")
        selected = matches[0]
        if selected.file_size > max_archive_bytes:
            raise ValueError("Nested archive exceeds max_archive_bytes")
        with outer.open(selected) as source, inner_path.open("xb") as target:
            count = _copy(source, target, selected.file_size)
        if count != selected.file_size:
            raise ValueError("Nested archive size differs from its directory record")

    project = attempt / "project"
    with zipfile.ZipFile(inner_path) as inner:
        entries = _entries(inner, max_members, max_path_depth)
        files = [(member, parts) for member, parts in entries if not member.is_dir()]
        if (
            not files
            or sum(member.file_size for member, _ in files) > max_uncompressed_bytes
        ):
            raise ValueError("Project is empty or exceeds max_uncompressed_bytes")
        # Strip only one common wrapper; unwrapped project subdirectories stay intact.
        wrapper = (
            all(len(parts) > 1 for _, parts in files)
            and len({parts[0] for _, parts in files}) == 1
        )
        project.mkdir()
        for member, parts in files:
            relative = parts[1:] if wrapper else parts
            destination = project.joinpath(*relative)
            destination.resolve().relative_to(project.resolve())
            destination.parent.mkdir(parents=True, exist_ok=True)
            with inner.open(member) as source, destination.open("xb") as target:
                count = _copy(source, target, member.file_size)
                target.flush()
                os.fsync(target.fileno())
            if count != member.file_size:
                raise ValueError(f"Archive member size mismatch: {member.filename!r}")
    if not any(
        path.suffix.lower() == ".hms" and path.is_file() and path.stat().st_size
        for path in project.rglob("*")
    ):
        raise ValueError("Extracted project has no nonempty .hms project file")
    return project


def _inventory(root: Path) -> dict:
    if _linked(root) or not root.is_dir():
        raise ValueError("Cannot inventory a linked or non-directory model root")
    files = []
    for directory, directories, names in os.walk(root, followlinks=False):
        folder = Path(directory)
        for name in sorted(directories + names):
            path = folder / name
            if _linked(path):
                raise ValueError(f"Cannot inventory linked model material: {path.name}")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError(
                    f"Cannot inventory special model material: {path.name}"
                )
            before = path.stat()
            checksum, size = hashlib.sha256(), 0
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    checksum.update(block)
                    size += len(block)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ino,
            ):
                raise ValueError(f"Model changed during inventory: {path.name}")
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": size,
                    "sha256": checksum.hexdigest(),
                }
            )
    files.sort(key=lambda value: value["path"])
    return {
        "file_count": len(files),
        "bytes": sum(value["bytes"] for value in files),
        "files": files,
    }


def _record(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def _operation_lock(path: Path) -> Iterator[None]:
    try:
        stream = path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise RuntimeError(
            f"M3 intake lock exists; inspect active/stopped attempts before retrying: {path}"
        ) from exc
    try:
        with stream:
            stream.write(f"pid={os.getpid()}\n")
            stream.flush()
            yield
    finally:
        path.unlink()


def extract_project(
    url: str,
    root: Path,
    model_id: str,
    unit_id: str,
    *,
    overwrite: bool,
    max_archive_bytes: int,
    max_uncompressed_bytes: int,
    max_members: int,
    max_path_depth: int,
) -> Path:
    """Prepare an isolated project and preserve an inventoried prior destination."""
    for label, limit in (
        ("max_archive_bytes", max_archive_bytes),
        ("max_uncompressed_bytes", max_uncompressed_bytes),
        ("max_members", max_members),
        ("max_path_depth", max_path_depth),
    ):
        if type(limit) is not int or limit <= 0:
            raise ValueError(f"{label} must be a positive integer")
    if len(_parts(model_id)) != 1 or len(_parts(unit_id)) != 1:
        raise ValueError("Model and unit identifiers must each be one path component")
    root = root.resolve()
    parent = root / model_id
    parent.mkdir(parents=True, exist_ok=True)
    if _linked(parent):
        raise ValueError("M3 model output path cannot be a link or junction")
    destination = parent / unit_id
    destination.resolve().relative_to(root)
    with _operation_lock(parent / f".{unit_id}.intake.lock"):
        if destination.exists() or destination.is_symlink():
            if _linked(destination) or not destination.is_dir():
                raise ValueError("M3 project destination must be an ordinary directory")
            if not overwrite:
                return destination
        attempt = Path(tempfile.mkdtemp(prefix=f".{unit_id}.intake-", dir=parent))
        logger.info("M3 intake evidence and staging: %s", attempt)
        outer = attempt / "download.zip"
        _download(url, outer, max_archive_bytes)
        project = _extract(
            outer,
            attempt,
            model_id,
            unit_id,
            max_archive_bytes,
            max_uncompressed_bytes,
            max_members,
            max_path_depth,
        )
        if _linked(parent) or parent.resolve() != parent:
            raise ValueError("M3 model output path changed during intake")
        destination.resolve().relative_to(root)
        if destination.is_symlink() or (destination.exists() and _linked(destination)):
            raise ValueError("M3 project destination changed to a link during intake")
        record = {
            "schema": "hms-commander/m3-intake/1.0.0",
            "model_id": model_id,
            "unit_id": unit_id,
            "status": "prepared",
            "project": _inventory(project),
            "previous": None,
        }
        if destination.exists():
            record["previous"] = _inventory(destination)
        _record(attempt / "intake.json", record)
        previous = attempt / "previous"
        for location in (destination, project, previous):
            location.resolve().relative_to(root)
        if record["previous"] is not None:
            destination.rename(previous)
        try:
            project.rename(destination)
        except OSError:
            if previous.exists():
                try:
                    previous.rename(destination)
                except OSError as exc:
                    raise RuntimeError(
                        f"Publication and restoration failed; retained prior project: {previous}"
                    ) from exc
            raise
        record["status"] = "complete"
        _record(attempt / "intake.json", record)
        # These are only this attempt's downloaded archives, never prior model material.
        outer.unlink()
        (attempt / "project.zip").unlink()
        return destination
