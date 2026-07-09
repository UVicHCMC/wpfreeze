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


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, str, bytes]] = {}
    redirects: dict[str, str] = {}
    request_log: list[str] = []

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
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if write_body:
            self.wfile.write(body)

    def log_message(self, format, *args):  # silence default stderr logging
        pass


def _make_handler(routes, redirects):
    return type(
        "FixtureHandler",
        (_FixtureHandler,),
        {"routes": routes, "redirects": redirects, "request_log": []},
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
    the exact bytes served for each internal path (for hash assertions)."""

    def __init__(self):
        self.site_handler = None
        self.cdn_handler = None
        self._site_ctx = None
        self._cdn_ctx = None
        self.site_server = None
        self.cdn_server = None

    def __enter__(self) -> "FixtureSite":
        self._cdn_ctx = _run_server(_make_handler(self._cdn_routes(), {}), "127.0.0.2")
        self.cdn_server = self._cdn_ctx.__enter__()
        cdn_base = f"http://127.0.0.2:{self.cdn_server.server_port}"

        site_routes, site_redirects = self._site_routes(cdn_base)
        self._site_ctx = _run_server(_make_handler(site_routes, site_redirects), "127.0.0.1")
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
