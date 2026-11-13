"""Shared single-writer company lock; ownership is in-process, never environment based."""
from contextlib import contextmanager
import os
from pathlib import Path


def _windows():
    return os.name == 'nt'


class LockOwnership:
    def __init__(self, path, stream):
        self.path, self.stream, self.pid, self.active = path, stream, os.getpid(), True

    def require(self, path):
        if (not self.active or self.pid != os.getpid() or self.stream.closed
                or self.path != Path(path).resolve()):
            raise ValueError('company lock ownership required')


@contextmanager
def locked(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ValueError('company lock must not be a symlink')
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if _windows():
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('公司目录正在执行；禁止并发') from None
        ownership = LockOwnership(path.resolve(), stream)
        try:
            yield ownership
        finally:
            ownership.active = False
            stream.seek(0)
            if _windows():
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def company_lock_for(state_file, explicit=None):
    """Only the known direct .work-<scope>/state layout is recoverable.

    An explicit lock must equal that layout's lock, not a new arbitrary lock.
    Never unlink a lock; old/unrecognized layouts fail closed.
    """
    path = Path(state_file).expanduser().absolute()
    if path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink():
        raise ValueError('recovery paths must not be symlinks')
    import json
    state = json.loads(path.read_bytes())
    scope = state.get('scope')
    if (not isinstance(scope, str) or not scope or '/' in scope or '\\' in scope
            or path.parent.name != '.work-' + scope
            or not path.name.endswith('.asset-inventory-state.json')):
        raise ValueError('unknown company runtime layout; cannot establish company lock')
    expected = path.parent.parent / '.company-run.lock'
    if explicit is not None and Path(explicit).expanduser().absolute() != expected:
        raise ValueError('company lock does not match state runtime')
    if expected.is_symlink():
        raise ValueError('company lock must not be a symlink')
    return expected
