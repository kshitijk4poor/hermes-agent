"""Immutable local-media references for owner-only native admission.

The existing managed document cache supplies profile routing, delivery eligibility
and upload limits. Its flat age-based cleanup skips this retained subdirectory.
There is deliberately no GC until it can account for queued AND unknown rows.
"""
import hashlib
import os
from pathlib import Path
import stat
import tempfile

from hermes_state_runtime import RuntimeStoreError


def _media_root():
    from gateway.platforms.base import get_document_cache_dir
    return get_document_cache_dir().resolve() / 'native-inputs'


def _sync_directory(path):
    # Windows cannot open directories through os.open; file fsync still precedes ACK.
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _open_regular(path):
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('native media must be a local regular file')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError('native media must be a regular file')
    return os.fdopen(fd, 'rb')


def capture_native_media(paths):
    from gateway.platforms.base import get_inbound_media_max_bytes, validate_inbound_media_size
    references = []
    limit = max(0, get_inbound_media_max_bytes())
    for value in paths:
        path = Path(value)
        try:
            source = _open_regular(path)
        except (OSError, ValueError) as exc:
            raise RuntimeStoreError('invalid_params') from exc
        with source:
            root = _media_root()
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.capture-', dir=root)
            temporary = Path(name)
            try:
                digest, size = hashlib.sha256(), 0
                with os.fdopen(fd, 'wb') as output:
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        try:
                            validate_inbound_media_size(size, max_bytes=limit)
                        except ValueError as exc:
                            raise RuntimeStoreError('invalid_params') from exc
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                target = root / digest.hexdigest() / path.name
                target.parent.mkdir(mode=0o700, exist_ok=True)
                reference = {'path': str(target), 'sha256': digest.hexdigest(), 'size': size}
                if target.exists():
                    # An earlier admission can reference this file. Never repair it by
                    # overwriting accepted bytes, even when a retry has the same digest.
                    restore_native_media([reference])
                else:
                    os.replace(temporary, target)
                for directory in (target.parent, root, root.parent, root.parent.parent, root.parent.parent.parent):
                    _sync_directory(directory)
                references.append(reference)
            finally:
                temporary.unlink(missing_ok=True)
    return references


def restore_native_media(references):
    if not references:
        return []
    root = _media_root()
    paths = []
    try:
        for reference in references:
            if set(reference) != {'path', 'sha256', 'size'}:
                raise ValueError('invalid media reference')
            path = Path(reference['path'])
            if (path.parent.parent != root or path.resolve() != path
                    or path.parent.name != reference['sha256']):
                raise ValueError('foreign media reference')
            with _open_regular(path) as source:
                if os.fstat(source.fileno()).st_size != reference['size']:
                    raise ValueError('changed media size')
                if hashlib.file_digest(source, 'sha256').hexdigest() != reference['sha256']:
                    raise ValueError('changed media bytes')
            paths.append(str(path))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeStoreError('storage_unavailable') from exc
    return paths
