"""HTTP/3-first runtime transport with shared QUIC capability and fallback state."""

import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import niquests
from niquests import RequestException as RequestException
from niquests import exceptions as exceptions
from niquests.structures import CaseInsensitiveDict, QuicSharedCache

USER_AGENT = 'AzurPilot/2.1'
BROWSER_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36'
)
DOWNLOAD_HEADERS = {
    'User-Agent': BROWSER_USER_AGENT,
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}
SERVICE_DOMAINS = ('azurpilot.com', 'nanoda.work')


def is_service_url(url):
    """Match the service roots and their subdomains, without suffix lookalikes."""
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower().rstrip('.')
    return parsed.scheme in ('http', 'https') and any(
        host == domain or host.endswith('.' + domain) for domain in SERVICE_DOMAINS
    )


_QUIC_CACHE = QuicSharedCache(max_size=12288)


def _prepare(url, headers, download):
    headers = CaseInsensitiveDict(headers or {})
    if download:
        # Preserve functional overrides such as Range and Accept-Encoding: identity.
        for key, value in DOWNLOAD_HEADERS.items():
            headers.setdefault(key, value)
        headers['User-Agent'] = BROWSER_USER_AGENT
    elif is_service_url(url):
        headers['User-Agent'] = USER_AGENT
    if is_service_url(url):
        parsed = urlsplit(url)
        if parsed.scheme == 'https':
            _QUIC_CACHE.add_domain(parsed.hostname, port=parsed.port or 443)
    return headers


class Session(niquests.Session):
    def __init__(self, *, download=False, **kwargs):
        kwargs.setdefault('quic_cache_layer', _QUIC_CACHE)
        super().__init__(**kwargs)
        self.download = download

    def request(self, method, url, *, headers=None, download=None, **kwargs):
        headers = _prepare(url, headers, self.download if download is None else download)
        return super().request(method, url, headers=headers, **kwargs)


class AsyncSession(niquests.AsyncSession):
    def __init__(self, *, download=False, **kwargs):
        kwargs.setdefault('quic_cache_layer', _QUIC_CACHE)
        super().__init__(**kwargs)
        self.download = download

    async def request(self, method, url, *, headers=None, download=None, **kwargs):
        headers = _prepare(url, headers, self.download if download is None else download)
        return await super().request(method, url, headers=headers, **kwargs)


def request(method, url, *, download=False, **kwargs):
    with Session(download=download) as session:
        return session.request(method, url, **kwargs)


def get(url, **kwargs):
    kwargs.setdefault('allow_redirects', True)
    return request('GET', url, **kwargs)


def post(url, **kwargs):
    return request('POST', url, **kwargs)


def head(url, **kwargs):
    kwargs.setdefault('allow_redirects', False)
    return request('HEAD', url, **kwargs)


def download_file(url, target, *, timeout=(5, 30)):
    """Atomically populate an upstream download cache without an Origin header."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size:
        return str(target)
    partial = None
    try:
        with get(url, download=True, stream=True, timeout=timeout,
                 headers={'Accept-Encoding': 'identity'}) as response:
            response.raise_for_status()
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.part', delete=False) as output:
                partial = Path(output.name)
                for chunk in response.iter_content(65536):
                    output.write(chunk)
            expected = response.headers.get('Content-Length')
            if expected is not None and partial.stat().st_size != int(expected):
                raise ValueError('Incomplete download')
        os.replace(partial, target)
    finally:
        if partial is not None:
            partial.unlink(missing_ok=True)
    return str(target)
