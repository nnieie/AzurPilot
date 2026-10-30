"""Bounded HTTP transport and explicit public API contracts."""

import re
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

from deploy.api_endpoints import API_BASE_URL, TEMPLATE_API_BASE_URL, TEMPLATE_CDN_BASE_URL

ITEM_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}$')


class CloudError(Exception):
    """A response could not safely be used; durable jobs remain queued."""

    def __init__(self, message, retry_after=30):
        super().__init__(message)
        self.retry_after = min(max(float(retry_after), 1), 86400)


def retry_after(value):
    try:
        seconds = float(value)
        return seconds if math.isfinite(seconds) else 30
    except (TypeError, ValueError):
        try:
            return (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 30


class PublicAPI:
    def __init__(self, device_id, *, main=API_BASE_URL, templates=TEMPLATE_API_BASE_URL,
                 cdn=TEMPLATE_CDN_BASE_URL, session=None):
        self.main = main.rstrip('/')
        self.templates = templates.rstrip('/')
        self.cdn = cdn.rstrip('/')
        self.session = session or requests.Session()
        self.headers = {'User-Agent': 'AzurPilot', 'X-AzurPilot-ID': device_id}

    def request(self, method, url, *, limit=8 << 20, **kwargs):
        headers = {**self.headers, **kwargs.pop('headers', {})}
        try:
            with self.session.request(method, url, headers=headers, timeout=(5, 20),
                                      allow_redirects=False, stream=True, **kwargs) as response:
                if response.status_code == 304:
                    return None, dict(response.headers)
                if response.status_code != 200:
                    raise CloudError(f'HTTP {response.status_code}',
                                     retry_after(response.headers.get('Retry-After')))
                data = bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise CloudError('Cloud response exceeds size limit')
                return bytes(data), dict(response.headers)
        except requests.RequestException as error:
            raise CloudError(type(error).__name__) from error

    def json(self, method, url, **kwargs):
        import json
        data, _ = self.request(method, url, **kwargs)
        try:
            result = json.loads(data)
        except (TypeError, ValueError) as error:
            raise CloudError('Invalid JSON response') from error
        if not isinstance(result, dict):
            raise CloudError('Expected a JSON object')
        return result

    def register(self, png_base64, revision, scene):
        return self.json('POST', self.templates + '/api/v3/post/templates/unknown',
                         limit=65536, json={'image_base64': png_base64,
                                           'client_revision': revision, 'scene': scene,
                                           'client': 'AzurPilot'})

    def register_many(self, candidates):
        import json
        if not 1 <= len(candidates) <= 50:
            raise ValueError('Template registration batches require 1 to 50 candidates')
        body = {'candidates': candidates}
        if len(json.dumps(body).encode()) > 256 << 10:
            raise ValueError('Template registration batch exceeds 256 KiB')
        return self.json('POST', self.templates + '/api/v3/post/unknown-candidates',
                         limit=1 << 20, json=body)

    def submit(self, drops):
        return self.json('POST', self.main + '/api/v3/post/drops', limit=1 << 20,
                         json={'drops': drops})

    def statistics(self, scene, *, options=False, **filters):
        if scene not in ('commission', 'research'):
            raise ValueError('Unsupported statistics scene')
        path = '/api/v3/get/' + scene + ('/options' if options else '')
        return self.json('GET', self.main + path, params=filters)

    def cdn_url(self, url):
        parsed, base = urlsplit(url), urlsplit(self.cdn)
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            raise CloudError('Template URL does not belong to the configured CDN')
        if parsed.query or parsed.fragment or not parsed.path.startswith('/releases/'):
            raise CloudError('Invalid immutable template URL')
        return url
