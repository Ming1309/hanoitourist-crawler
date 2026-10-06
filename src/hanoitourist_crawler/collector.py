"""Bounded, sequential browser acquisition with committed checkpoints."""

import re
import time
from hashlib import sha256
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from playwright.sync_api import sync_playwright

from .models import RawDocument, now
from .parser import inventory, merge_inventory, sources
from .selectors import DAY_BUTTONS, DIALOG, DIALOG_LINK, ROOT, SCHEDULE, SECTIONS


class CaptureError(RuntimeError):
    pass


class Collector:
    def __init__(self, config, storage, run_id):
        self.config, self.storage, self.run_id = config, storage, run_id
        self.manifest = {'actions': {}, 'inventory': {'sections': [], 'days': [], 'departures': {}, 'schedule_rows': []}}
        self.errors = []
        self.sequence = self.total_bytes = 0
        self.deadline = time.monotonic() + config.run_timeout
        self.node_id = ''
        self.state = 'initial'
        self.pending = set()
        self.blocked = set()
        self.fatal = None
        self.robots = None
        self.last_request = 0.0
        self.page = None

    def check(self):
        if self.fatal:
            raise CaptureError(self.fatal)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CaptureError('Run time budget exceeded')
        if self.page:
            self.page.set_default_timeout(min(self.config.navigation_timeout, remaining) * 1000)

    def save(self, kind, role, url, body, **metadata):
        self.check()
        if len(body) > self.config.max_document_bytes or self.total_bytes + len(body) > self.config.max_run_bytes:
            raise CaptureError('Raw body limit exceeded; body was not truncated or saved')
        document = RawDocument(self.run_id, self.sequence + 1, kind, role, self.state, url, body, **metadata)
        self.storage.append_raw(document)
        self.sequence += 1
        self.total_bytes += len(body)
        for html, prefix in sources(document):
            merge_inventory(self.manifest['inventory'], inventory(
                html, document=document, prefix=prefix, source_tour_id=self.node_id))
        self.storage.update_run(self.run_id, manifest=self.manifest)
        return document

    def load_robots(self):
        url = 'https://hanoitourist.vn/robots.txt'
        with httpx.Client(timeout=self.config.navigation_timeout, headers={'User-Agent': self.config.user_agent}) as client:
            response = client.get(url)
        self.save('robots', 'robots', url, response.content, http_status=response.status_code,
                  content_type=response.headers.get('content-type', 'text/plain'))
        if response.status_code != 200 or not re.search(r'^\s*User-agent\s*:', response.text, re.M | re.I):
            raise CaptureError('robots.txt unavailable or not recognizable: fail closed')
        self.robots = RobotFileParser(url)
        self.robots.parse(response.text.splitlines())
        if not self.robots.can_fetch(self.config.user_agent, self.config.tour_url):
            raise CaptureError('Tour URL denied by robots.txt')

    def data_path(self, url):
        u = urlsplit(url)
        if u.netloc != 'hanoitourist.vn' or u.scheme != 'https':
            return False
        if u.path.rstrip('/') == urlsplit(self.config.tour_url).path:
            return True
        return bool(self.node_id and (u.path == f'/tour/{self.node_id}/lich-khoi-hanh' or u.path == '/views/ajax'))

    def route(self, route):
        request = route.request
        url = request.url
        u = urlsplit(url)
        ajax_dialog = (self.node_id and u.netloc == 'hanoitourist.vn'
                       and u.path == f'/tour/{self.node_id}/lich-khoi-hanh'
                       and parse_qs(u.query).get('_wrapper_format') == ['drupal_dialog'])
        body = parse_qs(request.post_data or '')
        readonly_post = request.method == 'POST' and (
            (ajax_dialog and body.get('_drupal_ajax') == ['1'] and not any(k in body for k in ('form_id', 'op')))
            or (u.path == '/views/ajax' and u.netloc == 'hanoitourist.vn'
                and body.get('view_args') == [self.node_id] and body.get('view_name', [''])[0] in {'lich', 'tour'}))
        resource = request.resource_type in {'script', 'stylesheet', 'image', 'font', 'media'}
        blocked = (request.method not in {'GET', 'HEAD'} and not readonly_post)
        blocked |= any(host in u.netloc for host in ('google-analytics', 'googletagmanager', 'facebook', 'zalo.me'))
        blocked |= bool(re.search(r'/booking|/comment/reply/|statistics\.php|/admin/|/user/', u.path))
        blocked |= not resource and not self.data_path(url)
        if u.path == '/views/ajax' and request.method in {'GET', 'HEAD'}:
            query = parse_qs(u.query)
            blocked |= (query.get('view_args') != [self.node_id]
                        or query.get('view_name', [''])[0] not in {'lich', 'tour'})
        if blocked:
            self.blocked.add(request)
            route.abort()
            return
        if u.netloc == 'hanoitourist.vn' and self.robots and not self.robots.can_fetch(self.config.user_agent, url):
            self.blocked.add(request)
            if not resource:
                self.fatal = f'Data URL denied by robots.txt: {u.path}'
            route.abort()
            return
        if not resource:
            remaining = self.config.request_interval - (time.monotonic() - self.last_request)
            if remaining > 0:
                time.sleep(min(remaining, max(0, self.deadline - time.monotonic())))
            self.last_request = time.monotonic()
            self.pending.add(request)
        route.continue_()

    def response(self, response):
        request = response.request
        if not self.data_path(response.url) or request.resource_type in {'script','stylesheet','image','font','media'}:
            return
        try:
            body = response.body()
            kind = 'http_document' if request.resource_type == 'document' else 'ajax'
            self.save(kind, 'tour' if response.url.split('?')[0] == self.config.tour_url else 'schedule',
                      response.url, body, method=request.method, http_status=response.status,
                      content_type=response.headers.get('content-type', 'text/html'),
                      headers={k: v for k, v in response.headers.items()
                               if k in {'content-type', 'date', 'last-modified', 'etag'}})
            if kind == 'http_document' and not self.node_id:
                match = re.search(rb'data-history-node-id="(\d+)"', body)
                self.node_id = match[1].decode() if match else ''
            if response.status in {401, 403}:
                self.fatal = f'Access denied HTTP {response.status}; no bypass attempted'
            elif response.status >= 400 and self.state != 'initial':
                self.errors.append(f'Data response HTTP {response.status} in {self.state}')
        except Exception as exc:
            self.fatal = f'Unable to stage response ({type(exc).__name__}); stopping'
        finally:
            self.pending.discard(request)

    def failed_request(self, request):
        self.pending.discard(request)
        if request not in self.blocked and self.data_path(request.url):
            self.errors.append(f'Data request failed in {self.state}')

    def settle(self, scope):
        previous = None
        stable = 0
        for _ in range(100):
            self.check()
            html = scope.evaluate('(e) => e.outerHTML')
            signature = sha256(html.encode()).hexdigest()
            stable = stable + 1 if signature == previous and not self.pending else 0
            if stable >= 2:
                return
            previous = signature
            self.page.wait_for_timeout(200)
        raise CaptureError('Relevant content did not stabilize')

    def checkpoint(self, key, scope=None):
        self.check()
        self.state = key
        self.manifest['actions'][key] = 'discovered'
        if len(self.manifest['actions']) > self.config.max_states:
            raise CaptureError('State limit exceeded')
        scope = scope if scope is not None else self.page.locator(ROOT)
        self.settle(scope)
        self.save('dom', 'tour_state', self.page.url, self.page.content().encode())
        self.manifest['actions'][key] = 'captured'
        self.storage.update_run(self.run_id, manifest=self.manifest)

    def expand_prices(self, scope, label):
        toggles = scope.locator('table.footable > tbody > tr:not(.footable-detail-row) .footable-toggle')
        count = toggles.count()
        for index in range(count):
            self.check()
            row = toggles.nth(index).locator('xpath=ancestor::tr[1]')
            href = row.locator('a[href*="/booking"]').get_attribute('href') or str(index)
            key = f'{label}:price:{href}'
            self.manifest['actions'][key] = 'discovered'
            if 'footable-expanded' not in (row.get_attribute('class') or ''):
                toggles.nth(index).click()
            detail = row.locator('xpath=following-sibling::tr[1]').locator('.table-giave')
            detail.wait_for(state='visible')
            self.checkpoint(key, scope)
        if not count and scope.locator('td.ngay-khoi-hanh').count():
            tables = scope.locator('.table-giave')
            if not tables.count() or not all(t.is_visible() for t in tables.all()):
                raise CaptureError(f'Price expansion control unavailable: {label}')

    def traverse(self, scope, label):
        seen = set()
        for page_index in range(self.config.max_pages):
            self.check()
            signature = sha256(scope.inner_html().encode()).hexdigest()
            if signature in seen:
                raise CaptureError('Pager repeated a state')
            seen.add(signature)
            self.expand_prices(scope, f'{label}:{page_index}')
            self.checkpoint(f'{label}:page:{page_index}', scope)
            controls = scope.locator('select').all()
            if controls:
                self.select_combinations(scope, label, 0, ())
            if scope.locator('input:not([type=hidden]):not([type=button]):not([type=submit])').count():
                raise CaptureError('Unrecognized non-finite date/calendar input; needs adapter review')
            next_link = scope.locator('a[rel=next], .pager__item--next a')
            if not next_link.count() or not next_link.first.is_visible():
                return
            href = next_link.first.get_attribute('href') or ''
            if '/booking' in href:
                raise CaptureError('Unsafe pager target')
            next_link.first.click()
            self.settle(scope)
        raise CaptureError('Page limit exceeded')

    def select_combinations(self, scope, label, index, trail):
        selects = scope.locator('select')
        if index >= selects.count():
            self.expand_prices(scope, label + ':options:' + '|'.join(trail))
            self.checkpoint(label + ':options:' + '|'.join(trail), scope)
            return
        options = selects.nth(index).locator('option:not([disabled])').evaluate_all('(es)=>es.map(e=>e.value).filter(Boolean)')
        for option in options:
            self.check()
            selects.nth(index).select_option(option)
            self.settle(scope)
            self.select_combinations(scope, label, index + 1, trail + (option,))

    def collect(self):
        status = 'failed'
        try:
            self.load_robots()
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self.config.headless)
                try:
                    context = browser.new_context(locale='vi-VN', timezone_id='Asia/Ho_Chi_Minh',
                                                  viewport={'width': 1440, 'height': 1000},
                                                  user_agent=self.config.user_agent, service_workers='block')
                    context.route('**/*', self.route)
                    self.page = context.new_page()
                    self.page.set_default_timeout(self.config.navigation_timeout * 1000)
                    self.page.on('response', self.response)
                    self.page.on('requestfailed', self.failed_request)
                    for attempt in range(self.config.max_retries + 1):
                        self.check()
                        try:
                            response = self.page.goto(self.config.tour_url, wait_until='domcontentloaded')
                        except Exception:
                            if attempt == self.config.max_retries or self.fatal:
                                raise
                            continue
                        self.check()
                        if response and response.status < 400:
                            break
                        if not response or response.status not in {429, 500, 502, 503, 504} or attempt == self.config.max_retries:
                            raise CaptureError('Tour navigation failed')
                        retry = response.headers.get('retry-after', '1')
                        if retry.isdigit():
                            wait = float(retry)
                        else:
                            try:
                                wait = max(0, (parsedate_to_datetime(retry) - now()).total_seconds())
                            except (TypeError, ValueError):
                                wait = 1.0
                        if time.monotonic() + wait >= self.deadline:
                            raise CaptureError('Retry-After exceeds run budget')
                        self.page.wait_for_timeout(wait * 1000)
                    root = self.page.locator(ROOT)
                    root.wait_for()
                    self.page.locator('#lichkhoihanh td.ngay-khoi-hanh, #lichkhoihanh .view-empty').first.wait_for(state='attached')
                    self.checkpoint('tour:initial', root)
                    if self.page.locator('text=/captcha|access denied/i').count():
                        raise CaptureError('CAPTCHA/access gate detected; no bypass attempted')
                    for index, button in enumerate(self.page.locator(DAY_BUTTONS).all()):
                        key = f'day:{index + 1}'
                        self.manifest['actions'][key] = 'discovered'
                        target = button.get_attribute('data-bs-target')
                        if not target:
                            raise CaptureError('Accordion target missing')
                        if button.get_attribute('aria-expanded') != 'true':
                            button.click()
                        self.page.locator(target).wait_for(state='visible')
                        self.checkpoint(key, root)
                    self.traverse(self.page.locator(SCHEDULE), 'schedule')
                    link = self.page.locator(DIALOG_LINK)
                    if link.count():
                        self.state = 'dialog:open'
                        self.manifest['actions'][self.state] = 'discovered'
                        link.first.click()
                        dialog = self.page.locator(DIALOG).last
                        dialog.wait_for()
                        self.checkpoint('dialog:open', dialog)
                        self.traverse(dialog, 'dialog')
                    else:
                        raise CaptureError('Schedule dialog link missing')
                    for key in ('videos',):
                        scope = root.locator('#' + key)
                        if scope.count() and scope.locator('a[rel=next], .pager__item--next a').count():
                            self.traverse(scope, key)
                    review = root.locator('.sum-tour-comment a')
                    if review.count() and not re.match(r'\s*0\s', review.inner_text()):
                        raise CaptureError('Public review modal requires verified read-only adapter')
                    status = 'complete' if not self.errors else 'partial'
                finally:
                    browser.close()
        except KeyboardInterrupt:
            status = 'partial'
            self.errors.append('Interrupted; committed checkpoints retained')
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, CaptureError) else f'Capture failed ({type(exc).__name__})'
            self.errors.append(message)
            status = 'partial' if self.manifest['inventory']['sections'] else 'failed'
        finally:
            self.storage.update_run(self.run_id, final_url=self.page.url if self.page else self.config.tour_url,
                                    capture_status=status, manifest=self.manifest, errors=self.errors, finished_at=now())
        return status
