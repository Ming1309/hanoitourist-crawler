"""Conservative normalization: never infer a price or invent a missing value."""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo


def text(node):
    if node is None:
        return ''
    return re.sub(r'[ \t\xa0]+', ' ', node.get_text('\n', strip=True)).strip()


def money(raw):
    value = re.sub(r'\s+', '', raw)
    currency = 'VND' if re.search(r'(?:VNĐ|VND|đ)$', value, re.I) else None
    if not currency:
        return None, None
    number = re.sub(r'(?:VNĐ|VND|đ)$', '', value, flags=re.I)
    if not re.fullmatch(r'(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,3}(?:\.\d{3})+)', number):
        return None, currency
    try:
        return Decimal(number.replace(',', '').replace('.', '')).quantize(Decimal('.01')), currency
    except InvalidOperation:
        return None, currency


def integer(raw):
    return int(raw) if re.fullmatch(r'\d+', raw.strip()) else None


def departure_time(raw):
    for fmt in ('%d/%m/%Y %H:%M', '%d/%m/%Y'):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=ZoneInfo('Asia/Ho_Chi_Minh'))
        except ValueError:
            pass
    return None
