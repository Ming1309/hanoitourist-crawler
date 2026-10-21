import pytest

from hanoitourist_crawler.acquisition.scope import listing_url, tour_url

BASE = 'https://hanoitourist.vn/tour-trong-nuoc'


def test_relative_and_tracking():
    assert tour_url('/some-tour/?utm_source=card#details', BASE) == 'https://hanoitourist.vn/some-tour'
    assert listing_url('?page=1', BASE) == BASE + '?page=1'


@pytest.mark.parametrize('value', ['http://hanoitourist.vn/tour', 'https://hanoitourist.vn.evil/tour',
    'https://user@hanoitourist.vn/tour', '//example.com/tour', '/tour/1/lich/2/booking',
    '/admin', '/user', '/views/ajax', '/tour-trong-nuoc', '/tour?unknown=1'])
def test_unsafe_detail(value):
    with pytest.raises(ValueError):
        tour_url(value, BASE)


@pytest.mark.parametrize('value', ['?page=-1', '?page=1&page=2', '?search=x',
                                  '/tour-nuoc-ngoai?page=1', 'https://example.com/?page=1'])
def test_unsafe_pager(value):
    with pytest.raises(ValueError):
        listing_url(value, BASE)


def test_redirect_transport_keeps_valid_slash_and_tracking():
    from hanoitourist_crawler.acquisition.scope import redirect_url
    actual=redirect_url('/some-tour/?utm_source=card#details',BASE)
    assert actual=='https://hanoitourist.vn/some-tour/?utm_source=card'
    assert tour_url(actual)=='https://hanoitourist.vn/some-tour'
    assert redirect_url('?page=01',BASE,listing=True)==BASE+'?page=01'


@pytest.mark.parametrize('value',['','//evil.example/tour','http://hanoitourist.vn/tour',
    'https://user:password@hanoitourist.vn/tour','https://hanoitourist.vn:444/tour',
    '/booking','/tour-nuoc-ngoai','/tour?unknown=1'])
def test_redirect_rejects_instead_of_normalizing_unsafe_target(value):
    from hanoitourist_crawler.acquisition.scope import redirect_url
    with pytest.raises(ValueError): redirect_url(value,BASE)


@pytest.mark.parametrize('value',['/some-tour','/tour-nuoc-ngoai?page=1','?page=1&unknown=1'])
def test_listing_redirect_stays_in_listing_scope(value):
    from hanoitourist_crawler.acquisition.scope import redirect_url
    with pytest.raises(ValueError): redirect_url(value,BASE,listing=True)
