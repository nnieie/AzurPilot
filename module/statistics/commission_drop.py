"""Commission identity, page pairing and structured cloud records.

Catalog provenance is embedded in assets/stats/commission_names.json. Names are
resolved exactly after Unicode normalization; uncertain pairings remain local.
"""

import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from numbers import Integral
from pathlib import Path

from module.cloud.catalog import map_items
from module.logger import logger


@lru_cache(maxsize=1)
def catalog():
    return json.loads(Path('assets/stats/commission_names.json').read_text(encoding='utf-8'))['entries']


def normalize(value):
    value = unicodedata.normalize('NFKC', value).upper().replace('曰', '日')
    return re.sub(r'''[\s\\/_\-'"“”.,、，。()（）\[\]]''', '', value)


def resolve_name(value, server):
    server = 'cn' if server.startswith('cn') else server
    value = normalize(value)
    variants = {value, value[:-1] + 'I'} if value.endswith('1') else {value}
    matches = [entry for entry in catalog() if any(normalize(alias) in variants
                                                  for alias in entry['aliases'].get(server, []))]
    if len(matches) == 2 and server == 'jp':
        codes = {entry['code'] for entry in matches}
        for family in ('BIW', 'NYB'):
            if codes == {family + ' VIP Escort', family + ' Patrol Escort'}:
                return next(entry for entry in matches if entry['code'] == family + ' VIP Escort')
    if len(matches) != 1:
        raise ValueError('unknown_or_ambiguous_commission_name')
    return matches[0]


def revise_identity(code, items):
    """Disambiguate published names using reward ranges, without guessing OCR digits."""
    night = [('Small Merchant Escort', 'Coins', 260, 450),
             ('Medium Merchant Escort', 'Coins', 300, 550),
             ('Large Merchant Escort', 'Coins', 350, 660),
             ('Coastal Defense Patrol', 'Oil', 65, 105),
             ('Buoy Inspection', 'Oil', 100, 160),
             ('Frontier Defense Patrol', 'Oil', 150, 230)]
    result = code
    for item in items:
        name, amount = item['item'], item['amount']
        for expected, material, lower, upper in night:
            if code == expected and name == material and lower <= amount <= upper:
                result = code + ' N'
        if code in ('BIW VIP Escort', 'NYB VIP Escort') and (
                (name == 'Gems' and 50 <= amount <= 80) or (name == 'Oil' and 480 <= amount <= 720)):
            result = code.replace('VIP', 'Patrol')
        if name != 'Coins':
            continue
        if code.startswith('Cargo Defense'):
            for suffix, lower, upper in [('Ⅰ', 50, 80), ('Ⅱ', 85, 125), ('Ⅲ', 145, 225)]:
                if lower <= amount <= upper:
                    result = 'Cargo Defense ' + suffix
        for family in ('Forest Protection Commission', 'Vein Protection Commission'):
            if code == family + ' Ⅱ':
                if 20 <= amount <= 39:
                    result = family + ' Ⅰ'
                elif 86 <= amount <= 190:
                    result = family + ' Ⅲ'
            elif code == family + ' Ⅲ':
                if 20 <= amount <= 39:
                    result = family + ' Ⅰ'
                elif 41 <= amount <= 69:
                    result = family + ' Ⅱ'
    return result


def classify_frame(image):
    from module.azur_stats.assets import COMMISSION_DONE, COMMISSION_PERFECT
    from module.combat.assets import GET_ITEMS_1, GET_ITEMS_2, GET_ITEMS_3, GET_SHIP
    from module.commission.assets import EXP_INFO_S_REWARD
    from module.ui.assets import COMMISSION_CHECK

    for kind, marker in [('done', COMMISSION_DONE), ('perfect', COMMISSION_PERFECT)]:
        if marker.match_template_color(image, offset=(10, 10)):
            return kind
    if any(marker.match_template_color(image, offset=(5, 0))
           for marker in (GET_ITEMS_1, GET_ITEMS_2, GET_ITEMS_3)):
        return 'reward'
    for kind, marker in [('exp', EXP_INFO_S_REWARD), ('ship', GET_SHIP), ('exit', COMMISSION_CHECK)]:
        if marker.match_template_color(image, offset=(10, 10)):
            return kind
    return 'other'


class CommissionReader:
    def __init__(self, config):
        from module.azur_stats.assets import COMMISSION_NAME
        from module.commission.commission import CommissionAmount
        from module.ocr.ocr import Ocr
        from module.statistics.get_items import GetItemsStatistics
        from module.statistics.item import ItemGrid
        from module.statistics.cloud_upload import resolver_for

        server = 'cn' if config.SERVER.startswith('cn') else config.SERVER
        self.server = server
        self.ocr = Ocr(COMMISSION_NAME, lang={'cn': 'cnocr', 'en': 'ppocr_v6', 'jp': 'jp', 'tw': 'tw'}[server])
        self.stats = GetItemsStatistics()
        self.stats.grid = ItemGrid(None, {})
        self.stats.grid.load_template_folder('./assets/stats_commission_items')
        self.stats.grid.cloud_resolver = resolver_for(config, 'commission')
        self.stats.grid.amount_ocr = CommissionAmount([], threshold=96, name='Commission_cloud_amount')
        self.stats.amount_area = (50, 72, 94, 94)

    classify = staticmethod(classify_frame)

    def name(self, image):
        return resolve_name(self.ocr.ocr(image), self.server)

    def items(self, image):
        items = self.stats.stats_get_items(image, tag=True)
        if not items or any(isinstance(item.amount, bool) or not isinstance(item.amount, Integral)
                            or not 0 < item.amount <= 10**9 for item in items):
            raise ValueError('invalid_reward_amount')
        return [{'item': item.name, 'amount': int(item.amount),
                 'source_template_id': getattr(item, 'source_template_id', item.name),
                 'tag': getattr(item, 'tag', '') or ''} for item in items]


def parse_commissions(images, reader):
    """Each status opens a record; explicit boundaries close it. No inferred identity."""
    events, diagnostics, pending = [], [], None

    def finish():
        nonlocal pending
        current, pending = pending, None
        if current is None:
            return
        if current['count'] > 1 or current.get('error'):
            diagnostics.append(current.get('error') or 'multiple_reward_pages')
            return
        source = current.get('items') or [{'item': 'PlaceHolder', 'amount': 1, 'tag': ''}]
        identity = current['identity']
        code = revise_identity(identity['code'], source) if identity['code'] else identity['name']
        events.append({'comm': code, 'name': identity['name'], 'status': current['status'],
                       'source_items': source, 'items': map_items(source, [])})

    for image in images[:80]:
        kind = reader.classify(image)
        if kind in ('done', 'perfect'):
            finish()
            pending = {'status': int(kind == 'perfect'), 'count': 0}
            try:
                pending['identity'] = reader.name(image)
            except ValueError as error:
                pending['error'] = str(error)
        elif kind == 'reward':
            if pending is None:
                diagnostics.append('reward_without_commission')
                continue
            pending['count'] += 1
            try:
                pending['items'] = reader.items(image)
            except ValueError as error:
                pending['error'] = str(error)
        elif kind in ('exp', 'ship', 'exit'):
            finish()
        elif kind == 'other':
            if pending:
                pending['error'] = 'unclassified_frame'
            finish()
    finish()
    if len(images) > 80:
        diagnostics.append('too_many_frames')
    return events, diagnostics


def record_commission_drops(images, config, *, collected_at=None):
    from module.statistics.cloud_upload import enqueue_record, queue_for
    from module.statistics.cl1_database import db
    from module.cloud.files import write_json

    events, diagnostics = parse_commissions(images, CommissionReader(config))
    stamp = collected_at or datetime.now(timezone.utc).isoformat()
    for event in events:
        event['record_id'] = uuid.uuid4().hex
        event['collected_at'] = stamp
        amounts = {}
        for row in event['items']:
            amounts[row['item']] = amounts.get(row['item'], 0) + row['amount']
        local = {**event, 'items': amounts, 'item_entries': event['items']}
        db.add_cloud_commission_drop(config.config_name, local)
        try:
            enqueue_record(config, 'commission', **event)
        except Exception as error:
            logger.warning(f'[云端统计] 委托上报入队失败，本地记录已保留: {error}')
    if diagnostics:
        path = queue_for(config).root / 'diagnostics' / (uuid.uuid4().hex + '.json')
        write_json(path, {'collected_at': stamp, 'errors': diagnostics})
        logger.warning(f'[云端统计] 委托配对需核对: {diagnostics}')
    return events
