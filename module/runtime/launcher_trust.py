"""应用级启动器信任密钥和一次性登录票据。"""
import secrets
import threading
import time
from module.base.runtime_params import TOKEN_TTL_SECONDS

TRUST_SECRET_ENV = 'AP_WEBUI_TRUST_SECRET'


class LauncherTrust:
    def __init__(self, secret=None):
        self.secret = str(secret or '').strip()
        self.lock = threading.Lock()
        self.tokens = {}

    def check_secret(self, candidate):
        return bool(self.secret and isinstance(candidate, str)
                    and secrets.compare_digest(candidate.encode(), self.secret.encode()))

    def issue(self):
        with self.lock:
            now = time.monotonic()
            self.tokens = {key: expiry for key, expiry in self.tokens.items() if expiry > now}
            if len(self.tokens) >= 128:
                self.tokens.pop(next(iter(self.tokens)))
            token = secrets.token_urlsafe(32)
            self.tokens[token] = now + TOKEN_TTL_SECONDS
            return {'ticket': token, 'ttl': TOKEN_TTL_SECONDS}

    def consume(self, token):
        with self.lock:
            return self.tokens.pop(token, 0) > time.monotonic()

    def clear(self):
        with self.lock:
            self.tokens.clear()
