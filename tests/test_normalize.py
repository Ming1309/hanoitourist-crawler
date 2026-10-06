from decimal import Decimal

import pytest
from hanoitourist_crawler.normalize import departure_time, integer, money


@pytest.mark.parametrize('raw,amount', [('7,680,000 VNĐ','7680000.00'), ('1.150.000đ','1150000.00'), ('0đ','0.00')])
def test_money(raw, amount):
    assert money(raw) == (Decimal(amount), 'VND')


@pytest.mark.parametrize('raw', ['', 'Liên hệ', '1,23đ', '1.000,00đ', '950 USD'])
def test_ambiguous_or_missing_money(raw):
    assert money(raw)[0] is None


def test_dates_and_seats():
    assert departure_time('15/01/2027 12:00').isoformat() == '2027-01-15T12:00:00+07:00'
    assert departure_time('31/02/2027 12:00') is None
    assert integer('0') == 0
    assert integer('') is None
