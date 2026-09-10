"""On-demand stdio bridge; Perch credentials remain local to this MCP client."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import secrets
import threading
import time
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
import webbrowser

import anyio
import httpx
from mcp.client.auth import OAuthClientProvider
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken, OAuthMetadata, ProtectedResourceMetadata

from . import credentials, resources
from .storage import sync_lock

_JOBS = {}
_JOBS_LOCK = threading.Lock()


def _record(identifier):
    record = resources.catalog()['items'].get(identifier)
    if not record or record['kind'] != 'mcp' or record.get('deleted'):
        raise ValueError('This MCP server is not shared in Perch')
    return record


def _auth_key(record):
    return 'oauth-' + hashlib.sha256((record['id'] + record['config']['url']).encode()).hexdigest()


class TokenStore:
    def __init__(self, record):
        self.key = _auth_key(record)

    async def get_tokens(self):
        saved = credentials.read_secret(self.key + '-tokens')
        if not saved:
            return None
        token = dict(saved['token'])
        if token.get('expires_in') is not None:
            token['expires_in'] = max(0, int(saved['saved'] + token['expires_in'] - time.time()))
        return OAuthToken.model_validate(token)

    async def set_tokens(self, tokens):
        value = tokens.model_dump(mode='json')
        previous = credentials.read_secret(self.key + '-tokens')
        if not value.get('refresh_token') and previous:
            value['refresh_token'] = previous['token'].get('refresh_token')
        credentials.write_secret(self.key + '-tokens', {'saved': time.time(), 'token': value})

    async def get_client_info(self):
        value = credentials.read_secret(self.key + '-client')
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def set_client_info(self, client_info):
        credentials.write_secret(self.key + '-client', client_info.model_dump(mode='json'))


class PerchOAuthProvider(OAuthClientProvider):
    async def _initialize(self):
        await super()._initialize()
        token = self.context.current_tokens
        if token and token.expires_in is not None:
            self.context.token_expiry_time = time.time() + token.expires_in - 1
        metadata = credentials.read_secret(self.context.storage.key + '-metadata')
        if metadata:
            self.context.oauth_metadata = OAuthMetadata.model_validate(metadata['oauth'])
            self.context.auth_server_url = metadata['issuer']
            if metadata.get('resource'):
                self.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(metadata['resource'])

    def persist_metadata(self):
        if self.context.oauth_metadata:
            metadata = {
                'oauth': self.context.oauth_metadata.model_dump(mode='json'),
                'issuer': self.context.auth_server_url,
                'resource': self.context.protected_resource_metadata.model_dump(mode='json') if self.context.protected_resource_metadata else None,
            }
            key = self.context.storage.key + '-metadata'
            if credentials.read_secret(key) != metadata:
                credentials.write_secret(key, metadata)


class Callback:
    def __init__(self, port=0, cancel=None):
        self.done = threading.Event()
        self.cancel = cancel or threading.Event()
        self.expected = None
        self.result = None
        callback = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                state = query.get('state', [''])[0]
                valid = parsed.path == '/callback' and callback.expected and secrets.compare_digest(state, callback.expected)
                if not valid:
                    self.send_error(400, 'Invalid callback')
                    return
                if 'code' in query:
                    callback.result = (query['code'][0], state)
                elif 'error' in query:
                    callback.result = ValueError('Sign-in was declined')
                else:
                    self.send_error(400, 'Missing authorization code')
                    return
                callback.done.set()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(b'<p>You can return to Perch.</p>')

            def log_message(self, *_):
                pass

        self.server = HTTPServer(('127.0.0.1', port), Handler)
        self.uri = f'http://127.0.0.1:{self.server.server_port}/callback'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    async def redirect(self, url):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password:
            raise ValueError('The provider must use HTTPS for sign-in')
        self.expected = parse_qs(parsed.query).get('state', [None])[0]
        if not self.expected:
            raise ValueError('The provider did not supply an OAuth state')
        if not webbrowser.open(url):
            raise ValueError('Could not open the sign-in browser')

    async def receive(self):
        deadline = time.monotonic() + 300
        while not self.done.is_set():
            if self.cancel.is_set():
                raise ValueError('Sign-in cancelled')
            if time.monotonic() >= deadline:
                raise ValueError('Sign-in timed out')
            await asyncio.sleep(.1)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class SharedOAuth(httpx.Auth):
    """Reload tokens under a cross-process lock for every HTTP authentication flow."""
    requires_response_body = True

    def __init__(self, record, callback=None):
        self.record = record
        self.callback = callback

    async def async_auth_flow(self, request):
        deadline = time.monotonic() + 35
        while True:
            lock = sync_lock(resources.root() / 'auth-locks' / _auth_key(self.record), serialize_threads=False)
            try:
                lock.__enter__()
                break
            except ValueError:
                if time.monotonic() >= deadline:
                    raise ValueError('Another Perch sign-in or token refresh is running')
                await asyncio.sleep(.1)
        flow = None
        try:
            current = _record(self.record['id'])
            if _auth_key(current) != _auth_key(self.record):
                raise ValueError('The server changed; reconnect it in your harness')
            store = TokenStore(current)
            client = await store.get_client_info()
            redirect = self.callback.uri if self.callback else str(client.redirect_uris[0]) if client and client.redirect_uris else 'http://127.0.0.1/callback'
            async def no_browser(_):
                raise ValueError('Sign in to this server from Perch')
            if not self.callback and not await store.get_tokens():
                raise ValueError('Sign in to this server from Perch')
            provider = PerchOAuthProvider(
                server_url=current['config']['url'], storage=store,
                client_metadata=OAuthClientMetadata(client_name='Perch', redirect_uris=[redirect],
                                                   grant_types=['authorization_code', 'refresh_token'], response_types=['code']),
                redirect_handler=self.callback.redirect if self.callback else no_browser,
                callback_handler=self.callback.receive if self.callback else None,
            )
            flow = provider.async_auth_flow(request)
            outgoing = await anext(flow)
            while True:
                if outgoing.url.scheme != 'https':
                    raise ValueError('OAuth endpoints must use HTTPS')
                response = yield outgoing
                try:
                    outgoing = await flow.asend(response)
                except StopAsyncIteration:
                    break
            provider.persist_metadata()
        finally:
            try:
                if flow is not None:
                    await flow.aclose()
            finally:
                lock.__exit__(None, None, None)


@asynccontextmanager
async def upstream(record, callback=None):
    config = resources._material(record)
    if 'command' in config:
        from .term import external_process_env
        with external_process_env() as env:
            env.update(config.get('env', {}))
            async with stdio_client(StdioServerParameters(command=config['command'], args=config.get('args', []), env=env)) as streams:
                yield streams
    else:
        auth = SharedOAuth(record, callback) if record.get('authentication') == 'oauth' else None
        async with httpx.AsyncClient(auth=auth, headers=config.get('headers'), timeout=httpx.Timeout(30, read=300), follow_redirects=False) as client:
            async with streamable_http_client(config['url'], http_client=client) as (reader, writer, _):
                yield reader, writer


async def run_bridge(identifier):
    record = _record(identifier)
    async with stdio_server() as (down_reader, down_writer):
        async with upstream(record) as (up_reader, up_writer):
            async with anyio.create_task_group() as group:
                async def relay(reader, writer):
                    async for message in reader:
                        if isinstance(message, Exception):
                            raise ValueError('MCP connection interrupted')
                        _record(identifier)  # Removed resources stop forwarding immediately.
                        await writer.send(message)
                    group.cancel_scope.cancel()
                group.start_soon(relay, down_reader, up_writer)
                group.start_soon(relay, up_reader, down_writer)


def status(identifier):
    record = _record(identifier)
    if record.get('authentication') != 'oauth':
        return {'status': 'not-required'}
    saved = credentials.read_secret(_auth_key(record) + '-tokens')
    if not saved:
        return {'status': 'signed-out'}
    token = saved['token']
    expired = token.get('expires_in') is not None and saved['saved'] + token['expires_in'] <= time.time()
    return {'status': 'refresh-needed' if expired and token.get('refresh_token') else 'expired' if expired else 'signed-in'}


def sign_out(identifier):
    record = _record(identifier)
    with sync_lock(resources.root() / 'auth-locks' / _auth_key(record), serialize_threads=False):
        credentials.delete_secret(_auth_key(record) + '-tokens')
    return {'status': 'signed-out'}


def start_sign_in(identifier):
    record = _record(identifier)
    if record.get('authentication') != 'oauth':
        raise ValueError('Enable OAuth in this server’s settings first')
    with _JOBS_LOCK:
        for key, job in _JOBS.items():
            if job['resource'] == identifier and job['status'] == 'signing-in':
                return {'id': key, 'status': 'signing-in'}
        key = uuid4().hex
        cancel = threading.Event()
        _JOBS[key] = {'resource': identifier, 'status': 'signing-in', 'cancel': cancel}
        # Bound completed records; active sign-ins are never evicted.
        for old in list(_JOBS):
            if len(_JOBS) <= 30:
                break
            if _JOBS[old]['status'] != 'signing-in':
                del _JOBS[old]

    async def authorize():
        client = await TokenStore(record).get_client_info()
        port = 0
        if client and client.redirect_uris:
            uri = urlparse(str(client.redirect_uris[0]))
            if uri.hostname != '127.0.0.1' or uri.path != '/callback':
                raise ValueError('Saved OAuth client has an invalid callback')
            port = uri.port or 0
        callback = Callback(port, cancel)
        try:
            async with upstream(record, callback) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    if not await TokenStore(record).get_tokens():
                        raise ValueError('The server did not complete OAuth authorization')
        finally:
            callback.close()

    def work():
        try:
            asyncio.run(authorize())
            result = {'status': 'signed-in'}
        except Exception:
            result = {'status': 'cancelled' if cancel.is_set() else 'failed',
                      'error': 'Sign-in did not complete. Check provider support and try again.'}
        with _JOBS_LOCK:
            _JOBS[key].update(result)
    threading.Thread(target=work, daemon=True).start()
    return {'id': key, 'status': 'signing-in'}


def job_status(key, cancel=False):
    with _JOBS_LOCK:
        job = _JOBS.get(key)
        if not job:
            raise ValueError('Sign-in request not found')
        if cancel:
            job['cancel'].set()
        return {k: v for k, v in job.items() if k != 'cancel'}
