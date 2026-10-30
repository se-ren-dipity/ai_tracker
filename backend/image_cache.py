"""Bounded, on-demand copies of images already present in archived articles."""
import hashlib
import ipaddress
import logging
import os
import socket
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from lxml import html

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_CACHE_BYTES = 512 * 1024 * 1024
_slots = threading.BoundedSemaphore(4)
_image_locks = [threading.Lock() for _ in range(64)]
_disk_lock = threading.Lock()
_failures = OrderedDict()


def image_key(url):
    return hashlib.sha256(url.encode()).hexdigest()


def image_url(article_id, url):
    return f"/api/articles/{article_id}/images/{image_key(url)}"


def image_nodes(markup):
    return html.fragment_fromstring(markup or "<div></div>", create_parent="div")


def cached_article_html(article):
    if not article.content_html:
        return article.content_html
    root = image_nodes(article.content_html)
    for node in root.xpath(".//img[@src]"):
        node.set("src", image_url(article.id, node.get("src")))
        node.attrib.pop("srcset", None)
    return html.tostring(root, encoding="unicode")


def cache_dir():
    return Path(os.getenv("DB_PATH", "data/tracker.db")).resolve().parent / "image-cache"


def image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "image/avif"
    raise ValueError("unsupported-image")


def public_target(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 80, 443)):
        raise ValueError("invalid-image-url")
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise ValueError("invalid-image-url") from None
    port = url.port or (443 if url.scheme == "https" else 80)
    addresses = [ipaddress.ip_address(row[4][0]) for row in
                 socket.getaddrinfo(url.host, port, type=socket.SOCK_STREAM)]
    if not addresses:
        raise ValueError("blocked-image-address")
    for address in addresses:
        if (not address.is_global or address.is_multicast or address.is_reserved
                or (isinstance(address, ipaddress.IPv6Address)
                    and (address.ipv4_mapped or address.sixtofour or address.teredo))):
            raise ValueError("blocked-image-address")
    # Connect to the checked IP, never resolving the hostname a second time.
    address = min(addresses, key=lambda entry: entry.version)
    return url.copy_with(host=str(address)), url.netloc.decode("ascii"), url.host


def download_image(url):
    deadline = time.monotonic() + 30
    for redirect in range(4):
        target, host, hostname = public_target(url)
        if time.monotonic() >= deadline:
            raise TimeoutError("image-download-timeout")
        # A fresh client per hop avoids sharing cookies or TLS sessions across hosts.
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            with client.stream("GET", target,
                               headers={"Host": host, "User-Agent": "AITracker/1.0",
                                        "Accept": "image/*", "Accept-Encoding": "identity"},
                               extensions={"sni_hostname": hostname}) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    if redirect == 3 or not response.headers.get("location"):
                        raise ValueError("image-redirect-limit")
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("compressed-image-response")
                if int(response.headers.get("content-length", 0)) > MAX_IMAGE_BYTES:
                    raise ValueError("image-too-large")
                data = bytearray()
                for chunk in response.iter_raw():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("image-download-timeout")
                    if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                        raise ValueError("image-too-large")
                    data.extend(chunk)
                content = bytes(data)
                return content, image_type(content)
    raise ValueError("image-redirect-limit")


def get_cached_image(url):
    key = image_key(url)
    path = cache_dir() / f"{key}.img"
    with _slots, _image_locks[int(key[:2], 16) % len(_image_locks)]:
        with _disk_lock:
            if path.is_file():
                data = path.read_bytes()
                return data, image_type(data)
            failure = _failures.get(key)
            if failure and failure > time.monotonic():
                raise ValueError("image-retry-later")
        try:
            data, media_type = download_image(url)
            with _disk_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                files = sorted(path.parent.glob("*.img"), key=lambda entry: entry.stat().st_mtime)
                size = sum(entry.stat().st_size for entry in files)
                for entry in files:
                    if size + len(data) <= MAX_CACHE_BYTES:
                        break
                    size -= entry.stat().st_size
                    entry.unlink()
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as handle:
                        temporary = Path(handle.name)
                        handle.write(data)
                    temporary.replace(path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                _failures.pop(key, None)
            return data, media_type
        except (httpx.HTTPError, OSError, ValueError) as exc:
            with _disk_lock:
                _failures[key] = time.monotonic() + 60
                _failures.move_to_end(key)
                while len(_failures) > 256:
                    _failures.popitem(last=False)
            logger.warning("Image %s unavailable: %s", key[:12],
                           str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
            raise
