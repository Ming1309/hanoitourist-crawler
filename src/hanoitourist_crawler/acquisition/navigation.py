"""Single-hop acquisition; Chromium never receives an HTTP redirect to follow."""

from playwright.sync_api import Error

REDIRECTS = {301, 302, 303, 307, 308}
RESOURCES = {'script', 'stylesheet', 'image', 'font', 'media'}


class GuardedNavigation:
    def __init__(self, *, page, check, timeout, stage, redirect_target, approve,
                 record_redirect, block, finish, fail):
        self.page, self.check, self.timeout = page, check, timeout
        self.stage, self.redirect_target, self.approve = stage, redirect_target, approve
        self.record_redirect, self.block, self.finish, self.fail = record_redirect, block, finish, fail
        self.expected = None
        self.owner_request = None
        self.next_target = None
        self.visited = set()
        self.hops = 0
        self.last_document = None

    def owned(self, request):
        return (self.expected is not None and request.url == self.expected
                and request.resource_type == 'document' and request.method in {'GET', 'HEAD'}
                and request.frame == self.page().main_frame
                and self.owner_request in (None, request))

    def fetch(self, route):
        request = route.request
        response = None
        primary = self.owned(request)
        if primary:
            self.owner_request = request
        try:
            self.check()
            response = route.fetch(max_redirects=0, max_retries=0, timeout=self.timeout())
            self.check()
            body = response.body()
            self.check()
            document = self.stage(request, response.url, response.status, response.headers, body, primary)
            if primary:
                self.last_document = document
            if 300 <= response.status < 400:
                target, reason = None, 'blocked'
                try:
                    if response.status not in REDIRECTS or not primary:
                        raise ValueError('Unsupported or unowned redirect')
                    if self.hops >= 5:
                        raise ValueError('Redirect hop limit')
                    target = self.redirect_target(response.headers.get('location', ''), response.url)
                    self.check()
                    if target in self.visited:
                        raise ValueError('Redirect loop')
                    self.approve(target)
                    self.next_target = target
                    reason = 'approved'
                except ValueError:
                    self.fail('Redirect rejected by scope, ownership, robots or hop limit')
                self.record_redirect(request, response, document, target, reason)
                self.block(request)
                route.abort(error_code='aborted')
                return
            # APIResponse.body() is decoded; do not send stale compression/length
            # headers back to Chromium. Preserve status, MIME and Set-Cookie.
            headers = {k:v for k,v in response.headers.items()
                       if k.lower() not in {'content-encoding','content-length','transfer-encoding'}}
            route.fulfill(status=response.status, headers=headers, body=body)
        except Exception as exc:
            self.fail(f'Guarded acquisition failed ({type(exc).__name__})')
            self.block(request)
            route.abort(error_code='aborted')
        finally:
            self.finish(request)
            if response is not None:
                response.dispose()

    def navigate(self, url):
        if self.expected is not None:
            raise RuntimeError('Reentrant guarded navigation')
        self.visited = set()
        self.hops = 0
        self.last_document = None
        try:
            while True:
                self.check()
                self.expected, self.owner_request, self.next_target = url, None, None
                self.visited.add(url)
                try:
                    response = self.page().goto(url, wait_until='domcontentloaded')
                except Error as exc:
                    self.check()
                    if self.next_target is None or not str(exc).startswith('Page.goto: net::ERR_ABORTED'):
                        raise
                    url = self.next_target
                    self.hops += 1
                    continue
                self.check()
                if self.next_target is not None:
                    raise RuntimeError('Redirect abort was not acknowledged')
                return response
        finally:
            self.expected = self.owner_request = self.next_target = None
