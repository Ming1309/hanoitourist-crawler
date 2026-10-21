"""URL authority for verified domestic cards and finite listing pages."""

import re
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

DOMESTIC_LISTING_URL = 'https://hanoitourist.vn/tour-trong-nuoc'
RESERVED = {'admin', 'user', 'search', 'booking', 'views', 'tour-trong-nuoc', 'tour-nuoc-ngoai'}
TRACKING = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'fbclid', 'gclid'}


def _url(value, base):
    try:
        url = urlsplit(urljoin(base, value))
    except ValueError:
        raise ValueError('Invalid scoped URL') from None
    if url.scheme != 'https' or url.netloc != 'hanoitourist.vn' or url.username or url.password:
        raise ValueError('URL outside scoped HTTPS host')
    return url


def tour_url(value, base=DOMESTIC_LISTING_URL):
    if not value or not value.strip():
        raise ValueError('Missing tour URL')
    url = _url(value, base)
    path = url.path.rstrip('/')
    if not re.fullmatch(r'/[a-z0-9]+(?:-[a-z0-9]+)*', path) or path[1:] in RESERVED:
        raise ValueError('Unrecognized tour path')
    if any(key not in TRACKING for key, _ in parse_qsl(url.query, keep_blank_values=True)):
        raise ValueError('Unrecognized tour query')
    return urlunsplit(('https', 'hanoitourist.vn', path, '', ''))


def listing_url(value, base=DOMESTIC_LISTING_URL):
    url = _url(value, base)
    query = parse_qsl(url.query, keep_blank_values=True)
    if (url.path.rstrip('/') != '/tour-trong-nuoc' or url.fragment
            or len(query) > 1 or (query and (query[0][0] != 'page' or not re.fullmatch(r'\d+', query[0][1])))):
        raise ValueError('Unrecognized domestic pager')
    page = int(query[0][1]) if query else 0
    return DOMESTIC_LISTING_URL + (f'?page={page}' if page else '')


def redirect_url(value, base, *, listing=False):
    """Validate identity scope, but preserve the actual transport target."""
    if not value or not value.strip():
        raise ValueError('Missing redirect Location')
    target = _url(value, base)
    validator = listing_url if listing else tour_url
    validator(value, base)
    return urlunsplit((target.scheme, target.netloc, target.path, target.query, ''))
