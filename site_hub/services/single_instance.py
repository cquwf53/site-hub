from __future__ import annotations

import ctypes
import hashlib
import os
import sys
from pathlib import Path
from typing import BinaryIO


class SingleInstanceError(RuntimeError):
    pass


class SingleInstanceLock:
    """Process-lifetime lock scoped to one canonical SiteHub data directory."""

    def __init__(self, data_dir: Path) -> None:
        canonical = str(Path(data_dir).resolve()).casefold().encode("utf-8")
        self.key = hashlib.sha256(canonical).hexdigest()[:24]
        self.data_dir = Path(data_dir)
        self._handle: int | None = None
        self._stream: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None or self._stream is not None:
            return
        if sys.platform == "win32":
            self._acquire_windows()
        else:  # pragma: no cover - production is Windows; kept for developer tests.
            self._acquire_file()

    def _acquire_windows(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
        name = f"Local\\SiteHub-{self.key}"
        handle = kernel32.CreateMutexW(None, True, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            raise SingleInstanceError("another SiteHub instance already owns this data directory")
        self._handle = int(handle)

    def _acquire_file(self) -> None:
        import fcntl

        self.data_dir.mkdir(parents=True, exist_ok=True)
        stream = (self.data_dir / ".sitehub.instance.lock").open("a+b")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.close()
            raise SingleInstanceError(
                "another SiteHub instance already owns this data directory"
            ) from exc
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()).encode("ascii"))
        stream.flush()
        self._stream = stream

    def release(self) -> None:
        if self._handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex(ctypes.c_void_p(self._handle))
            kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None
        if self._stream is not None:  # pragma: no cover - non-Windows fallback.
            import fcntl

            path = Path(self._stream.name)
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None
            try:
                path.unlink()
            except OSError:
                pass

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


__all__ = ["SingleInstanceError", "SingleInstanceLock"]
