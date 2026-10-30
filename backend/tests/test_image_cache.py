import base64
import concurrent.futures
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lxml import html
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import database, image_cache as cache
from backend.models.article import Article
from backend.routers.articles import router

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
SOURCE = "https://images.example.com/photo.png"
CLIENT = httpx.Client


def resolved(address="93.184.216.34"):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


def response(content=PNG, **kwargs):
    return httpx.Response(stream=httpx.ByteStream(content), **{"status_code": 200, **kwargs})


class DownloadTests(unittest.TestCase):
    def transport(self, handler):
        return patch.object(cache.httpx, "Client", side_effect=lambda **kwargs:
                            CLIENT(transport=httpx.MockTransport(handler), **kwargs))

    def test_pins_validated_address_preserves_host_and_tls_name(self):
        requests = []
        def handler(request):
            requests.append(request)
            return response()
        with patch.object(cache.socket, "getaddrinfo", return_value=resolved()) as lookup, self.transport(handler):
            self.assertEqual(cache.download_image(SOURCE), (PNG, "image/png"))
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(requests[0].url.host, "93.184.216.34")
        self.assertEqual(requests[0].headers["host"], "images.example.com")
        self.assertEqual(requests[0].extensions["sni_hostname"], "images.example.com")
        self.assertNotIn("cookie", requests[0].headers)
        self.assertNotIn("authorization", requests[0].headers)

    def test_rejects_unsafe_urls_and_private_or_mixed_dns_before_network(self):
        for url in ["file:///etc/passwd", "http://user:pass@example.com/x", "http://example.com:8080/x", "https://example.com/\x00"]:
            with self.subTest(url=url), patch.object(cache.socket, "getaddrinfo") as lookup:
                with self.assertRaises(ValueError):
                    cache.public_target(url)
                lookup.assert_not_called()
        for address in ["127.0.0.1", "10.17.152.3", "169.254.169.254", "100.64.0.1",
                        "0.0.0.0", "224.0.0.1", "::1", "fc00::1", "::ffff:127.0.0.1"]:
            with self.subTest(address=address), patch.object(cache.socket, "getaddrinfo", return_value=resolved(address)):
                with self.assertRaises(ValueError):
                    cache.public_target(SOURCE)
        with patch.object(cache.socket, "getaddrinfo", return_value=resolved()+resolved("10.0.0.1")):
            with self.assertRaises(ValueError):
                cache.public_target(SOURCE)

    def test_revalidates_redirect_and_never_contacts_private_target(self):
        requests = []
        def handler(request):
            requests.append(request)
            return response(b"", status_code=302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        with patch.object(cache.socket, "getaddrinfo", side_effect=[resolved(), resolved("169.254.169.254")]), self.transport(handler):
            with self.assertRaisesRegex(ValueError, "blocked-image-address"):
                cache.download_image(SOURCE)
        self.assertEqual(len(requests), 1)

    def test_relative_redirect_uses_original_host_and_limit_is_bounded(self):
        requests = []
        def handler(request):
            requests.append(request)
            return response(b"", status_code=302, headers={"location": "/other.png"}) if len(requests) == 1 else response()
        with patch.object(cache.socket, "getaddrinfo", return_value=resolved()), self.transport(handler):
            cache.download_image(SOURCE)
        self.assertEqual(requests[-1].url.path, "/other.png")
        self.assertEqual(requests[-1].headers["host"], "images.example.com")
        with patch.object(cache.socket, "getaddrinfo", return_value=resolved()), self.transport(
                lambda req: response(b"", status_code=302, headers={"location": SOURCE})):
            with self.assertRaisesRegex(ValueError, "redirect-limit"):
                cache.download_image(SOURCE)

    def test_rejects_nonimages_compression_and_oversize_with_or_without_length(self):
        cases = [(b"<html>error</html>", {}), (b"<svg></svg>", {}),
                 (PNG, {"content-encoding": "gzip"}), (PNG, {"content-length": str(cache.MAX_IMAGE_BYTES+1)}),
                 (b"x" * 100, {})]
        for content, headers in cases:
            with self.subTest(headers=headers, start=content[:10]), \
                 patch.object(cache.socket, "getaddrinfo", return_value=resolved()), \
                 patch.object(cache, "MAX_IMAGE_BYTES", 80), \
                 self.transport(lambda req: response(content, headers=headers)):
                with self.assertRaises(ValueError):
                    cache.download_image(SOURCE)

    def test_slow_stream_cannot_extend_transfer_budget_forever(self):
        class SlowStream(httpx.SyncByteStream):
            def __iter__(self):
                yield PNG[:8]
                yield PNG[8:]
        with patch.object(cache.socket, "getaddrinfo", return_value=resolved()), \
             self.transport(lambda req: httpx.Response(200, stream=SlowStream())), \
             patch.object(cache.time, "monotonic", side_effect=[0, 1, 2, 31]):
            with self.assertRaises(TimeoutError):
                cache.download_image(SOURCE)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.setting = patch.object(cache, "cache_dir", return_value=Path(self.directory.name))
        self.setting.start()
        self.addCleanup(self.setting.stop)
        cache._failures.clear()

    def test_download_is_persistent_and_serves_when_source_goes_offline(self):
        with patch.object(cache, "download_image", return_value=(PNG, "image/png")) as download:
            first = cache.get_cached_image(SOURCE)
            download.side_effect = httpx.ConnectError("offline")
            self.assertEqual(cache.get_cached_image(SOURCE), first)
            download.assert_called_once()
        self.assertEqual(list(Path(self.directory.name).glob("*.tmp")), [])

    def test_same_image_is_downloaded_once_for_simultaneous_readers(self):
        def fetch(url):
            time.sleep(0.05)
            return PNG, "image/png"
        with patch.object(cache, "download_image", side_effect=fetch) as download:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(cache.get_cached_image, [SOURCE] * 4))
            self.assertEqual(results, [(PNG, "image/png")] * 4)
            download.assert_called_once()

    def test_failed_download_leaves_no_cache_and_has_retry_backoff(self):
        with patch.object(cache, "download_image", side_effect=httpx.ConnectError("offline")) as download:
            with self.assertRaises(httpx.ConnectError):
                cache.get_cached_image(SOURCE)
            with self.assertRaisesRegex(ValueError, "retry-later"):
                cache.get_cached_image(SOURCE)
            download.assert_called_once()
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_cache_evicts_oldest_file_before_exceeding_quota(self):
        with patch.object(cache, "MAX_CACHE_BYTES", len(PNG) * 2), \
             patch.object(cache, "download_image", return_value=(PNG, "image/png")):
            cache.get_cached_image(SOURCE)
            old = Path(self.directory.name) / (cache.image_key(SOURCE) + ".img")
            os.utime(old, (1, 1))
            cache.get_cached_image(SOURCE + "?two")
            cache.get_cached_image(SOURCE + "?three")
        self.assertFalse(old.exists())
        self.assertEqual(sum(p.stat().st_size for p in Path(self.directory.name).iterdir()), len(PNG) * 2)


class ImageApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.addCleanup(self.engine.dispose)
        Article.__table__.create(self.engine)
        self.session = sessionmaker(bind=self.engine)
        with self.session() as db:
            db.add(Article(id=1, title="News", url="https://news.example.com/1", content_html=f'<p>Body<img src="{SOURCE}"></p>'))
            db.add(Article(id=2, title="Other news", url="https://news.example.com/2", content_html="<p>No image</p>"))
            db.commit()
        app = FastAPI()
        app.include_router(router)
        def get_db():
            with self.session() as db:
                yield db
        app.dependency_overrides[database.get_db] = get_db
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_old_article_response_localizes_images_without_rewriting_database(self):
        with patch("backend.routers.articles.get_cached_image") as download:
            result = self.client.get("/api/articles/1").json()
            self.assertEqual(html.fromstring(result["content_html"]).xpath("//img/@src"), [cache.image_url(1, SOURCE)])
            download.assert_not_called()
        with self.session() as db:
            self.assertIn(SOURCE, db.get(Article, 1).content_html)

    def test_only_image_belonging_to_requested_article_can_be_fetched(self):
        with patch("backend.routers.articles.get_cached_image", return_value=(PNG, "image/png")) as download:
            for url in [cache.image_url(999, SOURCE), cache.image_url(2, SOURCE),
                        cache.image_url(1, "http://169.254.169.254/"), "/api/articles/1/images/bad"]:
                self.assertEqual(self.client.get(url).status_code, 404)
            download.assert_not_called()
            result = self.client.get(cache.image_url(1, SOURCE))
            self.assertEqual(result.content, PNG)
            self.assertEqual(result.headers["content-type"], "image/png")
            self.assertEqual(result.headers["x-content-type-options"], "nosniff")
            self.assertIn("max-age", result.headers["cache-control"])
            download.assert_called_once_with(SOURCE)

    def test_image_error_is_retryable_and_does_not_break_article_text(self):
        with patch("backend.routers.articles.get_cached_image", side_effect=ValueError("blocked-image-address")):
            response = self.client.get(cache.image_url(1, SOURCE))
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["retry-after"], "60")
            self.assertEqual(self.client.get("/api/articles/1").status_code, 200)


if __name__ == "__main__":
    unittest.main()
