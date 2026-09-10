"""OS credential storage, including bounded Windows Credential Manager entries."""
from contextlib import contextmanager
import hashlib
import json
import sys
from uuid import uuid4


def _backend():
    if sys.platform == 'darwin':
        from keyring.backends.macOS import Keyring
    elif sys.platform == 'win32':
        from keyring.backends.Windows import WinVaultKeyring as Keyring
    else:
        raise ValueError('Credential storage requires macOS Keychain or Windows Credential Manager')
    return Keyring()


def _service():
    from .resources import root
    return 'dev.soyr.perch.' + hashlib.sha256(str(root().resolve()).encode()).hexdigest()[:24]


@contextmanager
def _access(key):
    from .resources import root
    from .storage import sync_lock
    # Separate services prevent Windows' multi-user fallback from duplicating values.
    service = _service() + '.' + hashlib.sha256(key.encode()).hexdigest()[:32]
    with sync_lock(root() / 'credential-locks' / service):
        yield _backend(), service


def _forget(backend, service, key):
    from keyring.errors import PasswordDeleteError
    try:
        backend.delete_password(service, key)
    except PasswordDeleteError:
        pass


def _parts(backend, service):
    value = backend.get_password(service, 'index')
    if not value:
        return []
    index = json.loads(value)
    return [f'{index["generation"]}-{i}' for i in range(0, index['length'], 1000)]


def read_secret(key):
    try:
        with _access(key) as (backend, service):
            parts = _parts(backend, service)
            if not parts:
                return None
            values = [backend.get_password(service + '.' + part, 'value') for part in parts]
            if any(value is None for value in values):
                raise ValueError('Incomplete stored credential')
            return json.loads(''.join(values))
    except Exception as exc:
        raise ValueError('Cannot read the OS credential store') from exc


def write_secret(key, value):
    # ASCII chunks fit Windows' 2,560-byte UTF-16 credential limit, including JWTs.
    encoded = json.dumps(value, ensure_ascii=True)
    if len(encoded) > 250_000:
        raise ValueError('Credential data exceeds 250 KB')
    try:
        with _access(key) as (backend, service):
            previous = _parts(backend, service)
            generation = uuid4().hex
            parts = [f'{generation}-{i}' for i in range(0, len(encoded), 1000)]
            written = []
            try:
                for part, offset in zip(parts, range(0, len(encoded), 1000)):
                    backend.set_password(service + '.' + part, 'value', encoded[offset:offset+1000])
                    written.append(part)
                # A single small pointer commits the version; indexes themselves stay bounded.
                backend.set_password(service, 'index', json.dumps({'generation': generation, 'length': len(encoded)}))
            except Exception:
                for part in written:
                    _forget(backend, service + '.' + part, 'value')
                raise
            for part in previous:
                try:
                    _forget(backend, service + '.' + part, 'value')
                except Exception:
                    pass  # Committed value is valid even if old credential cleanup fails.
    except Exception as exc:
        raise ValueError('Cannot save to the OS credential store') from exc


def delete_secret(key):
    try:
        with _access(key) as (backend, service):
            parts = _parts(backend, service)
            _forget(backend, service, 'index')
            for part in parts:
                _forget(backend, service + '.' + part, 'value')
    except Exception as exc:
        raise ValueError('Cannot remove the saved credential') from exc
