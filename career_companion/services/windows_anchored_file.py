"""Windows handle-based reads for hash-bound local artifacts.

This module is imported only on Windows.  It deliberately uses Win32 handles instead of
``Path.read_bytes`` so every directory component and the final file can be opened without
following reparse points, held against rename/delete, and checked again after the bounded
read.
"""

from __future__ import annotations

import ctypes
import hashlib
import ntpath
import os
from ctypes import wintypes
from pathlib import Path


_GENERIC_READ = 0x80000000
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
_FILE_TYPE_DISK = 0x0001
_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_READ_CHUNK_BYTES = 1024 * 1024


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("low", wintypes.DWORD),
        ("high", wintypes.DWORD),
    ]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation_time", _FileTime),
        ("last_access_time", _FileTime),
        ("last_write_time", _FileTime),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_size_t),
        ("internal_high", ctypes.c_size_t),
        ("offset", wintypes.DWORD),
        ("offset_high", wintypes.DWORD),
        ("event", wintypes.HANDLE),
    ]


class _Kernel32:
    def __init__(self) -> None:
        if os.name != "nt":  # pragma: no cover - imported only by the Windows dispatch
            raise RuntimeError("Windows anchored reads require the Win32 API")
        library = ctypes.WinDLL("kernel32", use_last_error=True)
        self.create_file = library.CreateFileW
        self.create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.create_file.restype = wintypes.HANDLE

        self.close_handle = library.CloseHandle
        self.close_handle.argtypes = [wintypes.HANDLE]
        self.close_handle.restype = wintypes.BOOL

        self.get_file_information = library.GetFileInformationByHandle
        self.get_file_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        self.get_file_information.restype = wintypes.BOOL

        self.get_file_type = library.GetFileType
        self.get_file_type.argtypes = [wintypes.HANDLE]
        self.get_file_type.restype = wintypes.DWORD

        self.get_final_path = library.GetFinalPathNameByHandleW
        self.get_final_path.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self.get_final_path.restype = wintypes.DWORD

        self.lock_file = library.LockFileEx
        self.lock_file.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_Overlapped),
        ]
        self.lock_file.restype = wintypes.BOOL

        self.unlock_file = library.UnlockFileEx
        self.unlock_file.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_Overlapped),
        ]
        self.unlock_file.restype = wintypes.BOOL

        self.read_file = library.ReadFile
        self.read_file.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        self.read_file.restype = wintypes.BOOL


def _raise_last_error(message: str) -> None:
    error = ctypes.get_last_error()
    raise OSError(error, message, None, error)


def _safe_component(value: str) -> str:
    if not value or value in {".", ".."} or ntpath.basename(value) != value:
        raise ValueError("Unsafe local artifact path component")
    if any(character in value for character in ("/", "\\", ":")):
        raise ValueError("Unsafe local artifact path component")
    return value


def _absolute_path(value: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(value)))
    if not absolute.is_absolute() or not absolute.anchor:
        raise RuntimeError("The trusted artifact root must be an absolute Windows path")
    return absolute


def _extended_path(path: Path) -> str:
    normalized = ntpath.normpath(os.fspath(path))
    if normalized.startswith("\\\\?\\"):
        return normalized
    if normalized.startswith("\\\\"):
        return "\\\\?\\UNC\\" + normalized[2:]
    return "\\\\?\\" + normalized


def _open_handle(
    api: _Kernel32,
    path: Path,
    *,
    directory: bool,
) -> int:
    access = _FILE_READ_ATTRIBUTES if directory else _GENERIC_READ | _FILE_READ_ATTRIBUTES
    sharing = (
        _FILE_SHARE_READ | _FILE_SHARE_WRITE
        if directory
        else _FILE_SHARE_READ
    )
    flags = _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS
    if not directory:
        flags |= _FILE_FLAG_SEQUENTIAL_SCAN
    handle = api.create_file(
        _extended_path(path),
        access,
        sharing,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        _raise_last_error("A trusted local artifact path could not be opened")
    return int(handle)


def _close_handles(api: _Kernel32, handles: list[int]) -> None:
    while handles:
        api.close_handle(handles.pop())


def _information(api: _Kernel32, handle: int) -> _ByHandleFileInformation:
    information = _ByHandleFileInformation()
    if not api.get_file_information(handle, ctypes.byref(information)):
        _raise_last_error("A trusted local artifact identity could not be inspected")
    return information


def _timestamp(value: _FileTime) -> int:
    return (int(value.high) << 32) | int(value.low)


def _file_identity(information: _ByHandleFileInformation) -> tuple[int, int]:
    return (
        int(information.volume_serial_number),
        (int(information.file_index_high) << 32) | int(information.file_index_low),
    )


def _file_generation(
    information: _ByHandleFileInformation,
) -> tuple[int, int, int, int, int, int]:
    return (
        *_file_identity(information),
        (int(information.file_size_high) << 32) | int(information.file_size_low),
        _timestamp(information.creation_time),
        _timestamp(information.last_write_time),
        int(information.attributes),
    )


def _assert_directory(api: _Kernel32, handle: int) -> _ByHandleFileInformation:
    information = _information(api, handle)
    if information.attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise RuntimeError("Trusted local artifact paths cannot contain reparse points")
    if not information.attributes & _FILE_ATTRIBUTE_DIRECTORY:
        raise RuntimeError("A trusted local artifact parent is not a directory")
    if api.get_file_type(handle) != _FILE_TYPE_DISK:
        raise RuntimeError("Trusted local artifact directories must be disk-backed")
    return information


def _assert_regular_file(api: _Kernel32, handle: int) -> _ByHandleFileInformation:
    information = _information(api, handle)
    if information.attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise RuntimeError("Trusted local artifacts cannot be reparse points")
    if information.attributes & _FILE_ATTRIBUTE_DIRECTORY:
        raise RuntimeError("Trusted local artifacts must be regular files")
    if api.get_file_type(handle) != _FILE_TYPE_DISK:
        raise RuntimeError("Trusted local artifacts must be disk-backed regular files")
    return information


def _open_directory_chain(
    api: _Kernel32,
    absolute: Path,
) -> tuple[list[int], int]:
    current = Path(absolute.anchor)
    handles: list[int] = []
    try:
        components = absolute.parts[1:]
        if not components:
            handle = _open_handle(api, absolute, directory=True)
            _assert_directory(api, handle)
            handles.append(handle)
        else:
            for raw_component in components:
                current /= _safe_component(raw_component)
                handle = _open_handle(api, current, directory=True)
                _assert_directory(api, handle)
                handles.append(handle)
        return handles, handles[-1]
    except Exception:
        _close_handles(api, handles)
        raise


def _open_artifact_tree(
    api: _Kernel32,
    root: Path,
    parts: tuple[str, ...],
) -> tuple[list[int], int, int, int]:
    handles, root_handle = _open_directory_chain(api, root)
    parent_handle = root_handle
    current = root
    try:
        for raw_component in parts[:-1]:
            current /= _safe_component(raw_component)
            handle = _open_handle(api, current, directory=True)
            _assert_directory(api, handle)
            handles.append(handle)
            parent_handle = handle
        file_path = current / _safe_component(parts[-1])
        file_handle = _open_handle(api, file_path, directory=False)
        _assert_regular_file(api, file_handle)
        handles.append(file_handle)
        return handles, root_handle, parent_handle, file_handle
    except Exception:
        _close_handles(api, handles)
        raise


def _final_path(api: _Kernel32, handle: int) -> str:
    size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = api.get_final_path(handle, buffer, size, 0)
        if length == 0:
            _raise_last_error("A trusted local artifact path could not be resolved")
        if length < size:
            value = buffer.value
            break
        size = int(length) + 1
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value))


def _assert_beneath_root(api: _Kernel32, root_handle: int, file_handle: int) -> None:
    root_path = _final_path(api, root_handle)
    file_path = _final_path(api, file_handle)
    try:
        common = ntpath.commonpath((root_path, file_path))
    except ValueError as exc:
        raise RuntimeError("The local artifact escaped its trusted root") from exc
    if common != root_path or file_path == root_path:
        raise RuntimeError("The local artifact escaped its trusted root")


def _lock(api: _Kernel32, handle: int) -> _Overlapped:
    overlapped = _Overlapped()
    if not api.lock_file(
        handle,
        _LOCKFILE_FAIL_IMMEDIATELY,
        0,
        0xFFFFFFFF,
        0xFFFFFFFF,
        ctypes.byref(overlapped),
    ):
        _raise_last_error("The local artifact could not be locked for a trusted read")
    return overlapped


def _unlock(api: _Kernel32, handle: int, overlapped: _Overlapped) -> None:
    if not api.unlock_file(
        handle,
        0,
        0xFFFFFFFF,
        0xFFFFFFFF,
        ctypes.byref(overlapped),
    ):
        _raise_last_error("The trusted local artifact lock could not be released")


def _read_bytes(
    api: _Kernel32,
    handle: int,
    *,
    capture: bool,
    max_bytes: int,
) -> tuple[str, bytes | None]:
    digest = hashlib.sha256()
    chunks: list[bytes] | None = [] if capture else None
    total = 0
    while True:
        requested = min(_READ_CHUNK_BYTES, max_bytes - total + 1)
        buffer = ctypes.create_string_buffer(requested)
        received = wintypes.DWORD()
        if not api.read_file(
            handle,
            buffer,
            requested,
            ctypes.byref(received),
            None,
        ):
            _raise_last_error("The trusted local artifact could not be read")
        if received.value == 0:
            break
        block = buffer.raw[: received.value]
        total += len(block)
        if total > max_bytes:
            raise RuntimeError("The local artifact exceeds its bounded read limit")
        digest.update(block)
        if chunks is not None:
            chunks.append(block)
    return digest.hexdigest(), b"".join(chunks) if chunks is not None else None


def read_anchored_file(
    root: Path,
    relative_path: Path,
    *,
    capture: bool,
    max_bytes: int,
) -> tuple[str, bytes | None]:
    """Read one regular file beneath ``root`` using pinned, reparse-safe handles."""

    if max_bytes <= 0:
        raise ValueError("Anchored file reads require a positive byte limit")
    if relative_path.is_absolute() or not relative_path.parts:
        raise ValueError("Local artifact paths must be relative to the trusted root")
    parts = tuple(_safe_component(part) for part in relative_path.parts)
    absolute_root = _absolute_path(root)
    api = _Kernel32()
    handles: list[int] = []
    lock_state: _Overlapped | None = None
    file_handle = -1
    try:
        handles, root_handle, parent_handle, file_handle = _open_artifact_tree(
            api,
            absolute_root,
            parts,
        )
        _assert_beneath_root(api, root_handle, file_handle)
        root_identity = _file_identity(_information(api, root_handle))
        parent_identity = _file_identity(_information(api, parent_handle))
        lock_state = _lock(api, file_handle)
        file_information = _assert_regular_file(api, file_handle)
        if _file_generation(file_information)[2] > max_bytes:
            raise RuntimeError("The local artifact exceeds its bounded read limit")

        digest, content = _read_bytes(
            api,
            file_handle,
            capture=capture,
            max_bytes=max_bytes,
        )
        if _file_generation(_assert_regular_file(api, file_handle)) != _file_generation(
            file_information
        ):
            raise RuntimeError("The local artifact changed during its trusted read")

        fresh_handles: list[int] = []
        try:
            (
                fresh_handles,
                fresh_root_handle,
                fresh_parent_handle,
                fresh_file_handle,
            ) = _open_artifact_tree(api, absolute_root, parts)
            _assert_beneath_root(api, fresh_root_handle, fresh_file_handle)
            if root_identity != _file_identity(_information(api, fresh_root_handle)):
                raise RuntimeError("The trusted artifact root changed during access")
            if parent_identity != _file_identity(_information(api, fresh_parent_handle)):
                raise RuntimeError("The artifact parent changed during access")
            if _file_generation(file_information) != _file_generation(
                _assert_regular_file(api, fresh_file_handle)
            ):
                raise RuntimeError("The artifact file changed during access")
        finally:
            _close_handles(api, fresh_handles)
        return digest, content
    finally:
        try:
            if lock_state is not None and file_handle >= 0:
                _unlock(api, file_handle, lock_state)
        finally:
            _close_handles(api, handles)
