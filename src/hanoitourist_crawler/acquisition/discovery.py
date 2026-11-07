"""Bounded domestic discovery. Terminal pagination is not proof of absence."""

import re
import sys
import time
from dataclasses import replace
from email.utils import parsedate_to_datetime
from hashlib import sha256
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from .collector import CaptureError, Collector
from ..models import DiscoveredTour, DiscoveryResult, RawDocument, json_value, now
from ..processing.parser import _locator, evidence
from .scope import listing_url, tour_url, redirect_url
from .selectors import LISTING_ROOT, LISTING_ROWS, LISTING_TITLE, LISTING_NEXT


def listing_page(document):
    result = {'cards': [], 'errors': [], 'next_url': None, 'terminal': False}
    if document.http_status and document.http_status >= 400:
        result['errors'].append('Listing response failed')
        return result
    soup = BeautifulSoup(document.body.decode(document.encoding, errors='replace'), 'html.parser')
    roots = soup.select(LISTING_ROOT)
    if len(roots) != 1:
        result['errors'].append('Domestic listing scope missing or ambiguous')
        return result
    root = roots[0]
    rows = [row for row in root.select(LISTING_ROWS) if row.find_parent(class_='views-field-nothing') is None]
    for row in rows:
        link = row.select_one(LISTING_TITLE)
        url, error = '', ''
        try:
            url = tour_url(link.get('href', '') if link else '', document.url)
        except ValueError:
            error = 'unrecognized_tour_link'
        title = link.get_text(' ', strip=True) if link else ''
        if not title:
            error = 'missing_tour_title'
        schedule = row.select_one('.link-xem-lich a[href]')
        sid = None
        if schedule:
            target = urlsplit(urljoin(document.url, schedule['href']))
            match = re.fullmatch(r'/tour/(\d+)/lich-khoi-hanh', target.path)
            if target.scheme == 'https' and target.netloc == 'hanoitourist.vn' and match:
                sid = match[1]
        card = DiscoveredTour(url, title, sid, [evidence(document, _locator(row))], error)
        result['cards'].append(json_value(card))
        if error:
            result['errors'].append(f'Unresolved listing card: {error}; raw={document.id}')
    if not rows and not root.select_one('.view-empty'):
        result['errors'].append('No cards and no explicit domestic empty marker')
    next_links = root.select(LISTING_NEXT)
    if len(next_links) > 1:
        result['errors'].append('Ambiguous domestic pager')
    elif next_links:
        try:
            result['next_url'] = listing_url(next_links[0].get('href', ''), document.url)
        except ValueError:
            result['errors'].append('Unsafe domestic pager')
    else:
        result['terminal'] = not result['errors']
    if root.select('select, input:not([type=hidden])'):
        result['errors'].append('Unverified domestic listing control')
        result['terminal'] = False
    result['signature'] = sha256(str([(c['url'], c['source_tour_id']) for c in result['cards']]).encode()).hexdigest()
    return result


class DiscoveryCollector(Collector):
    """Shared listing scope, request policy and raw storage for HTML discovery."""

    def __init__(self, config, storage, run_id, *, pacer=None):
        self.original_config = config
        super().__init__(replace(config, tour_url=config.domestic_listing_url, run_timeout=config.discovery_timeout),
                         storage, run_id, pacer=pacer)
        self.manifest = {'actions': {}, 'pages': [], 'navigation': [], 'acquisition_version':'guarded-v1'}
        self.cards = []

    def data_role(self, kind, url):
        return 'listing'

    def redirect_target(self, location, source):
        target = redirect_url(location,source,listing=True)
        if not self.robots or not self.robots.can_fetch(self.config.user_agent,target):
            raise ValueError('Listing redirect denied by robots')
        return target

    def data_path(self, url):
        try:
            listing_url(url)
            return True
        except ValueError:
            return False

    def save(self, kind, role, url, body, **metadata):
        self.check()
        if len(body) > self.config.max_document_bytes or self.total_bytes + len(body) > self.config.max_run_bytes:
            raise CaptureError('Listing raw body limit exceeded; not truncated')
        doc = RawDocument(self.run_id, self.sequence + 1, kind, role, self.state, url, body, **metadata)
        self.storage.append_raw(doc)
        self.sequence += 1
        self.total_bytes += len(body)
        return doc

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        resource = request.resource_type in {'script', 'stylesheet', 'image', 'font', 'media'}
        unsafe = (request.method not in {'GET', 'HEAD'} or
                  bool(re.search(r'/booking|/comment/reply/|statistics\.php|/admin/|/user/', url.path)) or
                  any(host in url.netloc for host in ('google-analytics', 'googletagmanager', 'facebook', 'zalo.me')))
        if unsafe or (not resource and not self.data_path(request.url)):
            self.blocked.add(request)
            route.abort()
            return
        if url.netloc == 'hanoitourist.vn' and not self.robots.can_fetch(self.config.user_agent, request.url):
            if not resource:
                self.fatal = 'Listing data path denied by robots.txt'
            route.abort()
            return
        if not resource:
            self.pacer.wait(self.deadline)
            self.check()
        self.navigation.fetch(route)

class HTMLDiscoveryCollector(DiscoveryCollector):
    """Observe scoped server cards without claiming complete catalogue coverage."""

    def __init__(self, config, storage, run_id, *, pacer=None):
        super().__init__(config, storage, run_id, pacer=pacer)
        self.manifest.update(discovery_adapter='domestic-html-best-effort-v1',
            coverage_basis='guarded_http_document', coverage_guaranteed=False,
            source_pager_terminal_observed=False, stop_reason='', duplicate_card_count=0)
        self.errors.append('Best-effort HTML discovery; catalogue coverage is not guaranteed')

    def route(self, route):
        request = route.request
        if not self.navigation.owned(request):
            self.blocked.add(request)
            if request.resource_type == 'document' and request.frame == self.page.main_frame:
                self.fail_navigation('Unexpected unowned HTML listing navigation')
            route.abort()
            return
        super().route(route)

    def _close_browser(self, context, browser):
        original = sys.exception()
        cleanup_error = None
        try:
            if context is not None:
                # Keep interception installed until the context cannot issue requests.
                # unroute_all(wait) removes the guard before waiting for old callbacks.
                context.close()
        except Exception as exc:
            cleanup_error = exc
        finally:
            try:
                browser.close()
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            if original is None:
                raise cleanup_error
            # Cleanup must not mask the capture/DB failure or KeyboardInterrupt.
            self.errors.append(f'HTML browser cleanup failed ({type(cleanup_error).__name__})')

    def source_document(self, response):
        self.check()
        raw = self.navigation.last_document
        entry = self.manifest['navigation'][-1] if self.manifest['navigation'] else {}
        if (response is None or raw is None or raw.run_id != self.run_id
                or raw.kind != 'http_document' or raw.role != 'listing' or raw.method not in {'GET', 'HEAD'}
                or entry.get('owned_primary') is not True or entry.get('terminal') is not True
                or entry.get('raw_document_id') != raw.id or entry.get('url') != raw.url
                or entry.get('status') != raw.http_status or response.url != raw.url
                or response.status != raw.http_status):
            raise CaptureError('Missing or mismatched owned HTML listing evidence')
        if not 200 <= raw.http_status < 300 or raw.http_status == 204:
            raise CaptureError(f'Listing navigation HTTP {raw.http_status}; no bypass')
        if (raw.content_type.split(';', 1)[0].strip().lower() not in {'text/html', 'application/xhtml+xml'}
                or not raw.body.strip()):
            raise CaptureError('Listing response is not a nonempty HTML document')
        soup = BeautifulSoup(raw.body.decode(raw.encoding, errors='replace'), 'html.parser')
        if re.search(r'captcha|access denied', soup.get_text(' ', strip=True), re.I):
            raise CaptureError('Listing access gate; no bypass')
        if any(str(tag.get('http-equiv', '')).lower() == 'refresh' for tag in soup.select('meta[http-equiv]')):
            raise CaptureError('HTML listing meta-refresh is unsupported')
        return raw, soup

    def collect(self):
        status, observed = 'failed', False
        url = final_url = self.config.domestic_listing_url
        visited, signatures, card_urls = set(), set(), set()
        try:
            self.load_robots()
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self.config.headless)
                context = None
                try:
                    context = browser.new_context(locale='vi-VN', timezone_id='Asia/Ho_Chi_Minh',
                        viewport={'width':1440, 'height':1000}, user_agent=self.config.user_agent,
                        service_workers='block', java_script_enabled=False)
                    context.route('**/*', self.route)
                    self.page = context.new_page()
                    self.page.on('response', self.response)
                    self.page.on('requestfailed', self.failed_request)
                    for index in range(self.original_config.max_listing_pages):
                        self.check()
                        url = listing_url(url)
                        if url in visited:
                            raise CaptureError('Domestic pager repeated a URL')
                        visited.add(url)
                        self.state = f'listing:{index}'
                        self.manifest['actions'][self.state] = 'discovered'
                        if len(self.manifest['actions']) > self.config.max_states:
                            raise CaptureError('Listing state limit exceeded')
                        for attempt in range(self.config.max_retries + 1):
                            self.check()
                            response = self.navigate(url)
                            raw = self.navigation.last_document
                            if response is None or raw is None:
                                raise CaptureError('Missing guarded listing navigation response')
                            final_url = raw.url
                            if response.status not in {429, 500, 502, 503, 504} or attempt == self.config.max_retries:
                                break
                            retry = response.headers.get('retry-after', '1')
                            try:
                                wait = float(retry) if retry.isdigit() else max(0, (parsedate_to_datetime(retry) - now()).total_seconds())
                            except (ValueError, TypeError):
                                wait = 1.0
                            if wait >= min(self.deadline - time.monotonic(), self.wall_deadline - time.time()):
                                raise CaptureError('Listing Retry-After exceeds budget')
                            self.page.wait_for_timeout(wait * 1000)
                        raw, soup = self.source_document(response)
                        identity_url = listing_url(raw.url)
                        if identity_url != url:
                            if identity_url in visited:
                                raise CaptureError('Domestic redirect repeated a visited page')
                            visited.add(identity_url)
                        result = listing_page(raw)
                        roots = soup.select(LISTING_ROOT)
                        if len(roots) == 1 and roots[0].select('button, form, iframe'):
                            result['errors'].append('Unverified HTML listing control')
                            result['terminal'] = False
                        observed |= bool(result['cards']) or (len(roots) == 1 and roots[0].select_one('.view-empty') is not None)
                        for card in result['cards']:
                            if card['url'] and not card['identity_error']:
                                if card['url'] in card_urls:
                                    self.manifest['duplicate_card_count'] += 1
                                card_urls.add(card['url'])
                        result['raw_document_id'] = raw.id
                        result['url'] = raw.url
                        self.cards.extend(result['cards'])
                        self.manifest['pages'].append(result)
                        self.errors.extend(result['errors'])
                        self.manifest['actions'][self.state] = 'captured'
                        self.storage.update_run(self.run_id, manifest=self.manifest)
                        if result['errors']:
                            raise CaptureError('HTML listing observation has unsupported content')
                        if result['signature'] in signatures:
                            raise CaptureError('Domestic pager repeated an inventory state')
                        signatures.add(result['signature'])
                        if result['terminal']:
                            self.manifest['source_pager_terminal_observed'] = True
                            self.manifest['stop_reason'] = 'source_pager_end'
                            break
                        url = result['next_url']
                    if not self.manifest['source_pager_terminal_observed']:
                        raise CaptureError('Domestic listing page limit exceeded')
                finally:
                    self._close_browser(context, browser)
            self.check()
        except KeyboardInterrupt:
            status = 'partial'
            self.manifest['stop_reason'] = 'interrupted'
            self.errors.append('Interrupted domestic discovery; checkpoints retained')
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, CaptureError) else f'HTML listing failed ({type(exc).__name__})'
            self.manifest['stop_reason'] = message
            self.errors.append(message)
        finally:
            if observed:
                status = 'partial'
            self.manifest['observed_unique_url_count'] = len(card_urls)
            if self.manifest['duplicate_card_count']:
                self.errors.append(f'Observed repeated HTML listing cards: {self.manifest["duplicate_card_count"]}')
            self.storage.update_run(self.run_id, capture_status=status, final_url=final_url, finished_at=now(),
                                    manifest=self.manifest, errors=self.errors)
        return DiscoveryResult(status, self.cards, self.manifest['pages'], self.errors, False)


def discover_domestic(config, storage, discovery_run_id, *, pacer=None):
    return HTMLDiscoveryCollector(config, storage, discovery_run_id, pacer=pacer).collect()
