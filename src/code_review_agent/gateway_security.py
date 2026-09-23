"""Authentication and bounded ingress before multipart/JSON parsing."""
import asyncio
import hmac
import os
import time
from starlette.responses import JSONResponse

MAX_REQUEST_BYTES = 2 * 1024 * 1024 + 64 * 1024
BODY_TIMEOUT_SECONDS = 15

class GatewaySecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get('headers', [])}
        path = scope.get('path', '')
        async def reject(status, detail):
            response = JSONResponse({'detail': detail}, status_code=status,
                headers={'WWW-Authenticate': 'Bearer'} if status == 401 else None)
            await response(scope, receive, send)
        is_production = os.getenv('APP_ENV', '').strip().lower() in {'production', 'prod'}
        explicit_auth = os.getenv('REVIEW_REQUIRE_AUTH', '').strip().lower() in {'1', 'true', 'yes', 'on'}
        require_operator_auth = is_production or explicit_auth
        if require_operator_auth and (path == '/jobs' or path.startswith('/jobs/') or path.startswith('/api/')):
            expected = os.getenv('REVIEW_API_TOKEN', '')
            if len(expected) < 32:
                return await reject(503, 'Operator access is not configured. Set REVIEW_API_TOKEN to a random value of at least 32 characters on the server.')
            supplied = headers.get(b'authorization', b'')
            if not hmac.compare_digest(supplied, ('Bearer ' + expected).encode()):
                return await reject(401, 'Enter a valid operator access token to connect.')
            # One deployment = one trusted operator. No multi-tenant access is implied.
            scope.setdefault('state', {})['operator'] = 'operator'
        length = headers.get(b'content-length')
        if length:
            try:
                size = int(length)
                if size < 0:
                    raise ValueError()
            except ValueError:
                return await reject(400, 'Invalid Content-Length.')
            if size > MAX_REQUEST_BYTES:
                return await reject(413, 'Request exceeds the 2 MB upload limit plus form overhead.')
        if scope.get('method') in {'POST', 'PUT', 'PATCH'}:
            body = bytearray()
            deadline = time.monotonic() + BODY_TIMEOUT_SECONDS
            while True:
                try:
                    event = await asyncio.wait_for(receive(), timeout=max(0.001, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    return await reject(408, 'Request body timed out.')
                if event['type'] == 'http.disconnect':
                    return
                body.extend(event.get('body', b''))
                if len(body) > MAX_REQUEST_BYTES:
                    return await reject(413, 'Request body exceeds the upload limit.')
                if not event.get('more_body', False):
                    break
            consumed = False
            async def bounded_receive():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()
            return await self.app(scope, bounded_receive, send)
        return await self.app(scope, receive, send)
