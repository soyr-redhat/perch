"""Credential entry limits and interrupted replacement without native accounts."""
import tempfile
import unittest
from unittest.mock import patch

from keyring.errors import PasswordDeleteError
from perch import credentials, sync


class CredentialTests(unittest.TestCase):
    def test_large_values_and_failed_replacement_preserve_previous_credentials(self):
        values = {}
        class Backend:
            fail = False

            def get_password(self, service, key):
                return values.get((service, key))

            def set_password(self, service, key, value):
                if self.fail and key == 'index':
                    raise OSError('Injected credential failure')
                if len(value.encode('utf-16-le')) > 2560:
                    raise ValueError('Windows credential size exceeded')
                values[service, key] = value

            def delete_password(self, service, key):
                if (service, key) not in values:
                    raise PasswordDeleteError()
                del values[service, key]

        backend = Backend()
        with tempfile.TemporaryDirectory() as temp, patch.object(sync, 'PERCH_DIR', temp), patch.object(credentials, '_backend', return_value=backend):
            first = {'token': 'jwt' * 5000, 'unicode': '鳥' * 300}
            credentials.write_secret('server', first)
            self.assertEqual(credentials.read_secret('server'), first)
            count = len(values)
            backend.fail = True
            with self.assertRaises(ValueError):
                credentials.write_secret('server', {'token': 'replacement'})
            self.assertEqual(credentials.read_secret('server'), first)
            self.assertEqual(len(values), count)
            backend.fail = False
            credentials.write_secret('server', {'token': 'replacement'})
            self.assertEqual(credentials.read_secret('server'), {'token': 'replacement'})
            self.assertEqual(len(values), 2)
            credentials.delete_secret('server')
            self.assertIsNone(credentials.read_secret('server'))
            self.assertEqual(values, {})
