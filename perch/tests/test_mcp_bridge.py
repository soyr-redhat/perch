"""Real stdio forwarding and isolated OAuth storage, refresh, and callback checks."""
import asyncio
import base64
import hashlib
import copy
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from urllib.parse import parse_qs

import httpx
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from perch import credentials, mcp_bridge, resources, sync
from perch.storage import write_json


class MCPBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = patch.object(sync, 'PERCH_DIR', self.temp.name)
        p.start()
        self.addCleanup(p.stop)
        self.secrets = {}
        for name, fn in [('read_secret', lambda k: copy.deepcopy(self.secrets.get(k))),
                         ('write_secret', lambda k, v: self.secrets.__setitem__(k, copy.deepcopy(v))),
                         ('delete_secret', lambda k: self.secrets.pop(k, None))]:
            p = patch.object(credentials, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def record(self, config, authentication='none'):
        record = {'id': 'test-server', 'name': 'test-server', 'kind': 'mcp', 'config': config,
                  'targets': [], 'authentication': authentication, 'secrets': None}
        write_json(resources.root()/'resources.json', {'version': 1, 'items': {record['id']: record}})
        return record

    def test_two_harness_clients_use_the_bridge_without_the_desktop(self):
        self.record({'command': sys.executable, 'args': [str(Path(__file__).with_name('fixture_mcp.py'))]})
        async def client():
            params = StdioServerParameters(command=sys.executable, args=['-m', 'perch', '--mcp-bridge', 'test-server'],
                                           env={**os.environ, 'PERCH_DATA_DIR': self.temp.name})
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    self.assertEqual((await session.list_tools()).tools[0].name, 'echo')
                    result = await session.call_tool('echo', {'text': 'shared'})
                    self.assertEqual(result.content[0].text, 'Echo: shared')
                    self.assertEqual(str((await session.list_resources()).resources[0].uri), 'fixture://document')
                    self.assertIn('Fixture resource', (await session.read_resource('fixture://document')).contents[0].text)
                    self.assertEqual((await session.list_prompts()).prompts[0].name, 'review')
                    self.assertEqual((await session.get_prompt('review', {'subject': 'changes'})).messages[0].content.text, 'Review changes')
        async def both():
            await asyncio.wait_for(asyncio.gather(client(), client()), 25)
        asyncio.run(both())

    def test_callback_rejects_wrong_state_and_accepts_only_its_flow(self):
        callback = mcp_bridge.Callback()
        self.addCleanup(callback.close)
        callback.expected = 'expected-state'
        with self.assertRaises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(callback.uri+'?code=code&state=wrong', timeout=2)
        failure.exception.close()
        self.assertFalse(callback.done.is_set())
        urllib.request.urlopen(callback.uri+'?code=correct&state=expected-state', timeout=2).close()
        self.assertEqual(asyncio.run(callback.receive()), ('correct', 'expected-state'))

    def test_expiry_refresh_uses_saved_issuer_and_sign_out_blocks_existing_clients(self):
        record = self.record({'url': 'https://mcp.example/mcp'}, 'oauth')
        store = mcp_bridge.TokenStore(record)
        async def exercise():
            await store.set_client_info(OAuthClientInformationFull(client_id='perch', redirect_uris=['http://127.0.0.1:56789/callback']))
            await store.set_tokens(OAuthToken(access_token='old', refresh_token='refresh', token_type='Bearer', expires_in=1))
            self.secrets[store.key+'-tokens']['saved'] = time.time()-10
            self.secrets[store.key+'-metadata'] = {'oauth': {'issuer': 'https://auth.example', 'authorization_endpoint': 'https://auth.example/authorize',
                'token_endpoint': 'https://auth.example/refresh', 'response_types_supported': ['code']}, 'issuer': 'https://auth.example',
                'resource': {'resource': 'https://mcp.example/mcp', 'authorization_servers': ['https://auth.example']}}
            calls = []
            def serve(request):
                calls.append(str(request.url))
                if request.url.host == 'auth.example':
                    self.assertEqual(request.url.path, '/refresh')
                    return httpx.Response(200, json={'access_token': 'fresh', 'token_type': 'Bearer', 'expires_in': 3600})
                self.assertEqual(request.headers['authorization'], 'Bearer fresh')
                return httpx.Response(200, json={'ok': True})
            auth = mcp_bridge.SharedOAuth(record)
            async with httpx.AsyncClient(auth=auth, transport=httpx.MockTransport(serve)) as client:
                await asyncio.gather(client.get('https://mcp.example/mcp'), client.get('https://mcp.example/mcp'))
                self.assertEqual(calls.count('https://auth.example/refresh'), 1)
                self.assertEqual((await store.get_tokens()).refresh_token, 'refresh')
                mcp_bridge.sign_out(record['id'])
                with self.assertRaisesRegex(ValueError, 'Sign in'):
                    await client.get('https://mcp.example/mcp')
            other = dict(record, config={'url': 'https://other.example/mcp'})
            self.assertIsNone(await mcp_bridge.TokenStore(other).get_tokens())
        asyncio.run(exercise())

    def test_oauth_discovery_pkce_and_reuse_by_a_second_client(self):
        record = self.record({'url': 'https://mcp.example/mcp'}, 'oauth')
        store = mcp_bridge.TokenStore(record)
        authorization = {}
        requests = []

        class Browser:
            uri = 'http://127.0.0.1:56789/callback'

            async def redirect(inner, url):
                authorization.update(parse_qs(httpx.URL(url).query.decode()))
                self.assertEqual(authorization['code_challenge_method'], ['S256'])
                self.assertEqual(authorization['resource'], ['https://mcp.example/mcp'])

            async def receive(inner):
                return 'test-code', authorization['state'][0]

        def serve(request):
            requests.append(str(request.url))
            path = request.url.path
            if request.url.host == 'mcp.example' and path == '/mcp':
                if request.headers.get('authorization') == 'Bearer authorized':
                    return httpx.Response(200, json={'ok': True})
                return httpx.Response(401, headers={'WWW-Authenticate': 'Bearer resource_metadata="https://mcp.example/.well-known/oauth-protected-resource"'})
            if 'oauth-protected-resource' in path:
                return httpx.Response(200, json={'resource': 'https://mcp.example/mcp', 'authorization_servers': ['https://auth.example']})
            if 'oauth-authorization-server' in path or 'openid-configuration' in path:
                return httpx.Response(200, json={'issuer': 'https://auth.example', 'authorization_endpoint': 'https://auth.example/authorize',
                    'token_endpoint': 'https://auth.example/token', 'registration_endpoint': 'https://auth.example/register',
                    'response_types_supported': ['code'], 'code_challenge_methods_supported': ['S256']})
            if path == '/register':
                return httpx.Response(201, json={'client_id': 'perch-fixture', 'redirect_uris': [Browser.uri]})
            if path == '/token':
                form = parse_qs(request.content.decode())
                self.assertEqual(form['code'], ['test-code'])
                challenge = base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'][0].encode()).digest()).rstrip(b'=').decode()
                self.assertEqual(authorization['code_challenge'], [challenge])
                self.assertEqual(form['resource'], ['https://mcp.example/mcp'])
                return httpx.Response(200, json={'access_token': 'authorized', 'refresh_token': 'refresh', 'token_type': 'Bearer', 'expires_in': 3600})
            raise AssertionError(str(request.url))

        async def exercise():
            async with httpx.AsyncClient(auth=mcp_bridge.SharedOAuth(record, Browser()), transport=httpx.MockTransport(serve)) as client:
                self.assertEqual((await client.get(record['config']['url'])).status_code, 200)
            self.assertEqual((await store.get_tokens()).access_token, 'authorized')
            self.assertIn(store.key+'-metadata', self.secrets)
            before = len(requests)
            async with httpx.AsyncClient(auth=mcp_bridge.SharedOAuth(record), transport=httpx.MockTransport(serve)) as second:
                self.assertEqual((await second.get(record['config']['url'])).status_code, 200)
            self.assertEqual(requests[before:], [record['config']['url']])
        asyncio.run(exercise())
