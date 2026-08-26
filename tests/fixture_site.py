"""A tiny hand-built, WP-shaped site served locally for integration tests.

Two servers: the "site" (127.0.0.1, treated as internal) and an "external
cdn" (127.0.0.2, treated as external/not-owned) -- distinct loopback
addresses so host-based internal/external classification is exercised for
real rather than faked.
"""
from __future__ import annotations

import contextlib
import http.server
import threading
from collections import defaultdict


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, str, bytes]] = {}
    redirects: dict[str, str] = {}
    request_log: list[str] = []
    # Per-path status-code sequences, e.g. {"/limited/": [429, 429, 200]} --
    # sticks on the last entry once exhausted, same convention as
    # test_fetch.py's own scripted handler. Overrides only the status code
    # a path's route would otherwise return; content_type/body are unchanged.
    status_scripts: dict[str, list[int]] = {}
    # Extra response headers per path, e.g. {"/limited/": {"Retry-After": "1"}}
    # -- sent on every response to that path, script or not.
    extra_headers: dict[str, dict[str, str]] = {}
    call_counts: dict[str, int]

    def do_GET(self):  # noqa: N802
        self._handle(write_body=True)

    def do_HEAD(self):  # noqa: N802
        # A real server (Apache/Nginx/WordPress) answers HEAD like GET
        # minus the body -- probe_site's HEAD probes rely on that.
        self._handle(write_body=False)

    def _handle(self, write_body: bool) -> None:
        self.request_log.append(self.path)
        if self.path in self.redirects:
            self.send_response(302)
            self.send_header("Location", self.redirects[self.path])
            self.end_headers()
            return
        entry = self.routes.get(self.path)
        if entry is None:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if write_body:
                self.wfile.write(body)
            return
        status, content_type, body = entry
        script = self.status_scripts.get(self.path)
        if script:
            idx = min(self.call_counts[self.path], len(script) - 1)
            status = script[idx]
            self.call_counts[self.path] += 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for header_name, header_value in self.extra_headers.get(self.path, {}).items():
            self.send_header(header_name, header_value)
        self.end_headers()
        if write_body:
            self.wfile.write(body)

    def log_message(self, format, *args):  # silence default stderr logging
        pass


def _make_handler(routes, redirects, status_scripts=None, extra_headers=None):
    return type(
        "FixtureHandler",
        (_FixtureHandler,),
        {
            "routes": routes,
            "redirects": redirects,
            "request_log": [],
            "status_scripts": status_scripts or {},
            "extra_headers": extra_headers or {},
            "call_counts": defaultdict(int),
        },
    )


@contextlib.contextmanager
def _run_server(handler_class, host: str):
    server = http.server.ThreadingHTTPServer((host, 0), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()


class FixtureSite:
    """Context manager exposing `.site_base`, `.cdn_base`, request logs, and
    the exact bytes served for each internal path (for hash assertions).

    `status_scripts`/`extra_headers` apply to the *site* server only (not
    the CDN) -- see `_FixtureHandler` for their shape. Both default to
    empty, so every existing caller is unaffected; they exist so an
    integration test can make one path 429-then-200 (with a real
    Retry-After header) and drive a real `run_acquire` against it, to
    prove the rate-limit backoff works through the actual multi-threaded
    crawl pipeline -- not just fetch_with_retries/RateLimiter in
    isolation, which tests/test_fetch.py already covers thoroughly."""

    def __init__(
        self,
        status_scripts: dict[str, list[int]] | None = None,
        extra_headers: dict[str, dict[str, str]] | None = None,
    ):
        self.site_handler = None
        self.cdn_handler = None
        self._site_ctx = None
        self._cdn_ctx = None
        self.site_server = None
        self.cdn_server = None
        self._status_scripts = status_scripts or {}
        self._extra_headers = extra_headers or {}

    def __enter__(self) -> "FixtureSite":
        self._cdn_ctx = _run_server(_make_handler(self._cdn_routes(), {}), "127.0.0.2")
        self.cdn_server = self._cdn_ctx.__enter__()
        cdn_base = f"http://127.0.0.2:{self.cdn_server.server_port}"

        site_routes, site_redirects = self._site_routes(cdn_base)
        self._site_ctx = _run_server(
            _make_handler(site_routes, site_redirects, self._status_scripts, self._extra_headers),
            "127.0.0.1",
        )
        self.site_server = self._site_ctx.__enter__()
        self.site_base = f"http://127.0.0.1:{self.site_server.server_port}"
        self.cdn_base = cdn_base
        return self

    def __exit__(self, *exc_info):
        self._site_ctx.__exit__(*exc_info)
        self._cdn_ctx.__exit__(*exc_info)

    @property
    def site_request_log(self) -> list[str]:
        return self.site_server.RequestHandlerClass.request_log

    @property
    def cdn_request_log(self) -> list[str]:
        return self.cdn_server.RequestHandlerClass.request_log

    def _cdn_routes(self):
        return {
            "/logo.png": (200, "image/png", b"CDN-LOGO-BYTES"),
        }

    def _site_routes(self, cdn_base: str):
        index_html = f"""<!doctype html>
        <html><head>
        <link rel="canonical" href="/">
        <link rel="stylesheet" href="/style.css">
        </head><body>
        <a href="/about/">About</a>
        <a href="/blog/post-1/">Post 1</a>
        <a href="/old-page/">Old Page</a>
        <a href="/gone/">Gone</a>
        <a href="/secret/">Secret</a>
        <img src="{cdn_base}/logo.png">
        </body></html>"""

        about_html = """<!doctype html>
        <html><body><h1>About</h1></body></html>"""

        post1_html = """<!doctype html>
        <html><body>
        <h1>Post 1</h1>
        <img data-src="/wp-content/uploads/2024/photo.jpg" src="/img/placeholder.gif">
        </body></html>"""

        style_css = """
        .hero { background: url(/wp-content/uploads/2024/bg.png); }
        @import "/print.css";
        """

        print_css = ".print { color: black; }"

        routes = {
            "/": (200, "text/html", index_html.encode()),
            "/about/": (200, "text/html", about_html.encode()),
            "/blog/post-1/": (200, "text/html", post1_html.encode()),
            "/secret/": (403, "text/html", b"forbidden"),
            "/style.css": (200, "text/css", style_css.encode()),
            "/print.css": (200, "text/css", print_css.encode()),
            "/wp-content/uploads/2024/bg.png": (200, "image/png", b"BG-PNG-BYTES"),
            "/wp-content/uploads/2024/photo.jpg": (200, "image/jpeg", b"PHOTO-JPEG-BYTES"),
            "/img/placeholder.gif": (200, "image/gif", b"GIF89a-PLACEHOLDER"),
        }
        redirects = {
            "/old-page/": "/about/",
        }
        return routes, redirects


class MultisiteFixture:
    """A WordPress multisite *subdirectory* network, served locally.

    The shape is what matters:

      /courses/    the subsite being archived (base_url points here)
      /rocketry/     a sibling subsite -- same host, must stay out of scope
      /wp-content/   the network-wide theme and uploads, shared by every
                     subsite and therefore living OUTSIDE the archived
                     subsite's base_path
      cdn 127.0.0.2  a configured extra_host serving this site's assets

    That `/wp-content/` placement is the whole point. It is the one case
    where "owned host" and "in scope" genuinely disagree, so it is the only
    way to exercise the rule that HTML stays confined to base_path while
    CSS only has to be on an owned host. The stylesheet chain is two deep
    (style.css @imports print.css, and each references an upload) so the
    test proves recursive discovery through content that no page links to
    directly.

    `trailing_slash=False` serves the subsite root at "/courses" with no
    redirect, which is what makes _probe_trailing_slash return False. That
    is the only configuration in which in_scope's accept-the-base-with-or-
    without-its-slash handling is load-bearing, and no real site we have
    access to is configured that way.
    """

    def __init__(self, trailing_slash: bool = True):
        self.trailing_slash = trailing_slash
        self._site_ctx = None
        self._cdn_ctx = None
        self.site_server = None
        self.cdn_server = None

    def __enter__(self) -> "MultisiteFixture":
        self._cdn_ctx = _run_server(
            _make_handler({"/assets/app.js": (200, "application/javascript", b"CDN-APP-JS")}, {}),
            "127.0.0.2",
        )
        self.cdn_server = self._cdn_ctx.__enter__()
        self.cdn_base = f"http://127.0.0.2:{self.cdn_server.server_port}"

        routes, redirects = self._site_routes(self.cdn_base)
        self._site_ctx = _run_server(_make_handler(routes, redirects), "127.0.0.1")
        self.site_server = self._site_ctx.__enter__()
        self.site_base = f"http://127.0.0.1:{self.site_server.server_port}"
        self.base_url = f"{self.site_base}/courses/"
        return self

    def __exit__(self, *exc_info):
        self._site_ctx.__exit__(*exc_info)
        self._cdn_ctx.__exit__(*exc_info)

    @property
    def site_request_log(self) -> list[str]:
        return self.site_server.RequestHandlerClass.request_log

    @property
    def cdn_request_log(self) -> list[str]:
        return self.cdn_server.RequestHandlerClass.request_log

    def _site_routes(self, cdn_base: str):
        slash = "/" if self.trailing_slash else ""

        home_html = f"""<!doctype html>
        <html><head>
        <link rel="stylesheet" href="/wp-content/themes/demo/style.css">
        </head><body>
        <a href="/courses/about{slash}">About</a>
        <a href="/rocketry{slash}">Sibling lab</a>
        <iframe src="/otherlab/widget{slash}"></iframe>
        <script src="{cdn_base}/assets/app.js"></script>
        </body></html>"""

        about_html = """<!doctype html>
        <html><body><h1>About this subsite</h1></body></html>"""

        # A sibling subsite. Nothing here may ever be fetched: it shares the
        # hostname but is a different site, kept alive independently of this
        # one's static replacement.
        sibling_html = """<!doctype html>
        <html><body><h1>Rocketry</h1>
        <a href="/rocketry/members/">Members</a></body></html>"""

        # Network-wide theme CSS: outside base_path, on an owned host.
        style_css = """
        .hero { background: url(/wp-content/uploads/hero.jpg); }
        @import "/wp-content/themes/demo/print.css";
        """
        print_css = """
        @font-face { font-family: d; src: url(/wp-content/uploads/font.woff2); }
        """

        # An out-of-scope HTML page reached as a RENDER reference (an
        # iframe), not a hyperlink. It IS fetched -- "render even if
        # external, localize it" -- but must stay a leaf: following its
        # links is how one embedded page turns into another site's entire
        # graph. Distinct from /rocketry/, which is hyperlink-only and so
        # never fetched at all.
        # The <img> is the load-bearing part. A hyperlink out of an
        # out-of-scope page is rejected at admission anyway, so it cannot
        # show whether the page was parsed. A RENDER reference can: render
        # links are deliberately unconfined, so if this page is ever parsed
        # its image is fetched -- which is the cascade the leaf rule exists
        # to stop.
        widget_html = f"""<!doctype html>
        <html><body><p>widget</p>
        <img src="/otherlab/widget-asset.png">
        <a href="/otherlab/widget-deep{slash}">Deeper</a></body></html>"""

        routes = {
            f"/courses{slash}": (200, "text/html", home_html.encode()),
            f"/otherlab/widget{slash}": (200, "text/html", widget_html.encode()),
            f"/otherlab/widget-deep{slash}": (200, "text/html", b"<html><body>deep</body></html>"),
            "/otherlab/widget-asset.png": (200, "image/png", b"WIDGET-ASSET-PNG"),
            f"/courses/about{slash}": (200, "text/html", about_html.encode()),
            f"/rocketry{slash}": (200, "text/html", sibling_html.encode()),
            "/rocketry/members/": (200, "text/html", b"<html><body>members</body></html>"),
            "/wp-content/themes/demo/style.css": (200, "text/css", style_css.encode()),
            "/wp-content/themes/demo/print.css": (200, "text/css", print_css.encode()),
            "/wp-content/uploads/hero.jpg": (200, "image/jpeg", b"HERO-JPEG-BYTES"),
            "/wp-content/uploads/font.woff2": (200, "font/woff2", b"FONT-WOFF2-BYTES"),
        }
        # Whichever form the site prefers, a real server canonicalizes the
        # other one -- and that redirect is exactly what
        # _probe_trailing_slash reads. Without the inverse redirect here the
        # no-slash fixture would 404 on its own seed URL (base_url is always
        # "/"-terminated), which is a fixture bug rather than a finding.
        if self.trailing_slash:
            redirects = {"/courses": "/courses/", "/rocketry": "/rocketry/"}
        else:
            redirects = {
                "/courses/": "/courses",
                "/courses/about/": "/courses/about",
                "/rocketry/": "/rocketry",
            }
        return routes, redirects
