"""Pinned Ed25519 releases, verified icons and recoverable historical replacement."""

import base64
import hashlib
import json
import re

import cv2
import numpy as np
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from deploy.api_endpoints import TEMPLATE_PUBLIC_KEY
from module.cloud.files import atomic_write, read_json, write_json
from module.cloud.protocol import CloudError, ITEM_ID

HASH = re.compile(r'^[0-9a-f]{64}$')
DEFAULT_THRESHOLDS = {'formal': 0.92, 'cluster': 0.96, 'margin': 0.03, 'phash_distance': 6, 'color': 30}


def unpack_signed(data, key):
    try:
        envelope = json.loads(data)
        payload = base64.b64decode(envelope['payload_base64'], validate=True)
        signature = base64.b64decode(envelope['signature'], validate=True)
        key.verify(signature, payload)
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise ValueError('Expected object')
        return result
    except (InvalidSignature, KeyError, ValueError, TypeError) as error:
        raise CloudError('Invalid signed template document') from error


def decode_icon(png):
    if not png.startswith(b'\x89PNG\r\n\x1a\n'):
        raise CloudError('Template is not PNG')
    # Check dimensions before invoking the decoder, including malicious oversized IHDRs.
    if png[12:16] != b'IHDR' or len(png) < 33 or (
            int.from_bytes(png[16:20], 'big'), int.from_bytes(png[20:24], 'big')) != (49, 49):
        raise CloudError('Template must be 49 by 49 pixels')
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or image.dtype != np.uint8 or image.shape not in ((49, 49, 3), (49, 49, 4)):
        raise CloudError('Invalid template pixels')
    if image.shape[2] == 4 and np.any(image[:, :, 3] != 255):
        raise CloudError('Transparent template pixels are forbidden')
    return cv2.cvtColor(image[:, :, :3], cv2.COLOR_BGR2RGB)


def icon_features(image):
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    coefficients = cv2.dct(cv2.resize(gray, (32, 32)).astype(np.float32))[:8, :8].ravel()
    bits = coefficients > np.median(coefficients[1:])
    bits[0] = False
    fingerprint = sum(int(bit) << index for index, bit in enumerate(bits))
    return fingerprint, image.mean((0, 1)), float(np.sqrt(image.var((0, 1)).mean()))


def map_items(items, mappings):
    """Always derive from immutable source IDs, so corrected mappings remain reversible."""
    mapping = {row['unknown_id']: row for row in mappings}
    merged = {}
    for item in items:
        source = item.get('source_template_id', item['item'])
        row = mapping.get(source, {})
        key = row.get('item_id', item['item']), row.get('tag') or item.get('tag') or ''
        merged[key] = merged.get(key, 0) + item['amount']
    return [{'item': item, 'tag': tag, 'amount': amount}
            for (item, tag), amount in sorted(merged.items())]


class TemplateCatalog:
    def __init__(self, root, api, *, public_key=TEMPLATE_PUBLIC_KEY, apply_mappings=None):
        self.root, self.api = root, api
        self.key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        self.apply_mappings = apply_mappings or (lambda mappings: None)
        self.revision, self.templates, self.mappings = 0, [], []
        self.entries = []
        self.etag = None
        self.thresholds = dict(DEFAULT_THRESHOLDS)
        active = root / 'active.json'
        if active.exists():
            state = read_json(active)
            document = base64.b64decode(state['manifest'], validate=True)
            payload = unpack_signed(document, self.key)
            self._activate(payload)
            self.etag = state.get('etag')
        self.recover()

    def _validate(self, payload):
        revision = payload.get('revision')
        if type(revision) is not int or revision < 1:
            raise CloudError('Invalid template revision')
        entries, mappings = payload.get('templates'), payload.get('replace_ids')
        if not isinstance(entries, list) or len(entries) > 100000 or not isinstance(mappings, list):
            raise CloudError('Invalid template manifest')
        keys = set()
        for entry in entries:
            if (not isinstance(entry, dict) or not ITEM_ID.fullmatch(entry.get('template_id', ''))
                    or not ITEM_ID.fullmatch(entry.get('identity_id', ''))
                    or entry.get('kind') not in ('formal', 'temporary')
                    or not HASH.fullmatch(entry.get('sha256', ''))
                    or not HASH.fullmatch(entry.get('icon_sha256', ''))):
                raise CloudError('Invalid template entry')
            key = entry['identity_id'], entry['icon_sha256']
            if key in keys:
                raise CloudError('Duplicate template variant')
            keys.add(key)
            self.api.cdn_url(entry['url'])
        for row in mappings:
            if (not isinstance(row, dict) or not ITEM_ID.fullmatch(row.get('unknown_id', ''))
                    or not ITEM_ID.fullmatch(row.get('item_id', ''))
                    or not re.fullmatch(r'[A-Za-z0-9_-]{0,32}', row.get('tag', ''))):
                raise CloudError('Invalid replacement mapping')
        if len({row['unknown_id'] for row in mappings}) != len(mappings):
            raise CloudError('Duplicate replacement mapping')
        thresholds = {**DEFAULT_THRESHOLDS, **payload.get('match_thresholds', {})}
        for name, maximum in [('formal', 1), ('cluster', 1), ('margin', 1), ('color', 255), ('phash_distance', 64)]:
            value = thresholds[name]
            if type(value) not in (int, float) or not 0 <= value <= maximum:
                raise CloudError('Invalid template matching thresholds')

    def _icon(self, entry, *, download=False):
        path = self.root / 'icons' / (entry['sha256'] + '.png')
        png = path.read_bytes() if path.exists() else None
        if png is None and download:
            png, _ = self.api.request('GET', self.api.cdn_url(entry['url']), limit=350000)
        if png is None or hashlib.sha256(png).hexdigest() != entry['sha256']:
            raise CloudError('Template file checksum mismatch')
        image = decode_icon(png)
        if hashlib.sha256(image.tobytes()).hexdigest() != entry['icon_sha256']:
            raise CloudError('Template pixel checksum mismatch')
        if download and not path.exists():
            atomic_write(path, png)
        return image

    def _activate(self, payload):
        self._validate(payload)
        entries = []
        for entry in payload['templates']:
            image = self._icon(entry)
            entries.append((entry, image, *icon_features(image)))
        self.revision = payload['revision']
        self.templates = payload['templates']
        self.mappings = payload['replace_ids']
        self.entries = entries
        self.thresholds = {**DEFAULT_THRESHOLDS, **payload.get('match_thresholds', {})}

    def recover(self):
        journal = self.root / 'pending-release.json'
        if journal.exists():
            state = read_json(journal)
            payload = unpack_signed(base64.b64decode(state['manifest'], validate=True), self.key)
            self._validate(payload)
            if payload['revision'] < self.revision:
                raise CloudError('Pending release would roll back templates')
            # Validate every file before changing any historical records.
            for entry in payload['templates']:
                self._icon(entry)
            self.apply_mappings(payload['replace_ids'])
            write_json(self.root / 'active.json', state)
            self._activate(payload)
            self.etag = state.get('etag')
            journal.unlink()

    def refresh(self):
        """Adopt another local process's completed release before applying bindings."""
        self.recover()
        active = read_json(self.root / 'active.json')
        if active:
            payload = unpack_signed(base64.b64decode(active['manifest'], validate=True), self.key)
            if payload['revision'] > self.revision:
                self._activate(payload)
                self.etag = active.get('etag')

    def sync(self):
        self.refresh()
        headers = {'If-None-Match': self.etag} if self.etag else {}
        latest, response_headers = self.api.request('GET', self.api.cdn + '/latest.json',
                                                     limit=65536, headers=headers)
        if latest is None:
            return False
        pointer = unpack_signed(latest, self.key)
        revision = pointer.get('revision')
        if type(revision) is not int or revision < self.revision or revision < 1:
            raise CloudError('Template revision rollback or invalid revision')
        if revision == self.revision:
            return False
        document, _ = self.api.request('GET', self.api.cdn_url(pointer['manifest_url']), limit=64 << 20)
        if hashlib.sha256(document).hexdigest() != pointer.get('manifest_sha256'):
            raise CloudError('Manifest checksum mismatch')
        payload = unpack_signed(document, self.key)
        self._validate(payload)
        if payload['revision'] != revision:
            raise CloudError('Manifest revision mismatch')
        for entry in payload['templates']:
            self._icon(entry, download=True)
        write_json(self.root / 'pending-release.json', {
            'manifest': base64.b64encode(document).decode(),
            'etag': response_headers.get('ETag') or response_headers.get('etag'),
        })
        self.recover()
        return True

    def match(self, image, *, kind):
        scores = {}
        fingerprint, mean, stddev = icon_features(image)
        for entry, candidate, candidate_hash, candidate_mean, candidate_stddev in self.entries:
            if entry['kind'] != kind:
                continue
            if (fingerprint ^ candidate_hash).bit_count() > self.thresholds['phash_distance']:
                continue
            if np.max(np.abs(mean - candidate_mean)) > self.thresholds['color']:
                continue
            if min(stddev, candidate_stddev) < 5:
                score = 1 - float(np.sqrt(np.mean((image.astype(float) - candidate) ** 2))) / 255
            else:
                score = float(cv2.matchTemplate(image, candidate, cv2.TM_CCOEFF_NORMED)[0, 0])
            key = entry['template_id']
            if key not in scores or score > scores[key][0]:
                scores[key] = score, entry
        ranked = sorted(scores.values(), key=lambda row: row[0], reverse=True)
        if ranked and ranked[0][0] >= self.thresholds['formal' if kind == 'formal' else 'cluster']:
            if len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= self.thresholds['margin']:
                return ranked[0][1]
        return None
