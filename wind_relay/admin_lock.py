"""Serialize local configuration changes on Windows, macOS and Linux."""
from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def configuration_lock(gateway_path):
    path = Path(gateway_path).expanduser().absolute()
    lock_path = path.with_name(path.name + ".admin.lock")
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("Another administrator is changing this gateway configuration; retry after it finishes") from None
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("Another administrator is changing this gateway configuration; retry after it finishes") from None
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
