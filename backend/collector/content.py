"""Collect article bodies and keep only markup supported by our reader."""
import logging
import re
from datetime import datetime
from html import escape
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
import trafilatura
from lxml import html

logger = logging.getLogger(__name__)

ALLOWED_TAGS = {
    "div", "p", "h2", "h3", "h4", "ul", "ol", "li", "blockquote",
    "pre", "code", "strong", "em", "b", "i", "a", "br", "hr", "img",
    "figure", "figcaption", "table", "thead", "tbody", "tr", "th", "td",
    "details", "summary", "sup", "sub",
}
DROP_TAGS = {
    "script", "style", "noscript", "iframe", "object", "embed", "form",
    "input", "select", "textarea", "svg", "canvas", "nav", "aside", "footer",
}


def safe_url(value: str, base_url: str) -> str:
    url = urljoin(base_url, value.strip())
    return url if urlsplit(url).scheme in {"http", "https"} else ""


def sanitize_content(markup: str, base_url: str) -> str:
    """Preserve reading structure, discard source styling and executable markup."""
    root = html.fragment_fromstring(markup or "<p></p>", create_parent="div")
    for node in list(root.iterdescendants()):
        if not isinstance(node.tag, str) or node.tag.lower() in DROP_TAGS:
            node.drop_tree()
    # The reader supplies its own title.
    for node in root.xpath(".//h1"):
        node.drop_tree()
    anchors = {}
    for index, node in enumerate(root.xpath(".//h2 | .//h3 | .//h4"), 1):
        section_id = f"article-section-{index}"
        if node.get("id"):
            anchors[node.get("id")] = section_id
        node.set("id", section_id)
    for node in list(root.iterdescendants()):
        tag = node.tag.lower()
        if tag not in ALLOWED_TAGS:
            node.drop_tag()
            continue
        attrs = dict(node.attrib)
        node.attrib.clear()
        if tag in {"h2", "h3", "h4"}:
            node.set("id", attrs["id"])
        elif tag == "a":
            href = attrs.get("href", "")
            if href.startswith("#") and href[1:] in anchors:
                node.set("href", "#" + anchors[href[1:]])
            else:
                url = safe_url(href, base_url) if href else ""
                if url:
                    node.set("href", url)
                    node.set("target", "_blank")
                    node.set("rel", "noopener noreferrer")
        elif tag == "img":
            src = safe_url(attrs.get("src", ""), base_url) if attrs.get("src") else ""
            image_url = urlsplit(src)
            if image_url.hostname == "aihot.news" and image_url.path == "/api/img-proxy":
                original = parse_qs(image_url.query).get("u", [""])[0]
                if original:
                    src = safe_url(original, base_url)
            if not src:
                node.drop_tree()
                continue
            node.set("src", src)
            node.set("alt", attrs.get("alt", ""))
            node.set("loading", "lazy")
            node.set("referrerpolicy", "no-referrer")
        elif tag in {"td", "th"}:
            for key in ("colspan", "rowspan"):
                if re.fullmatch(r"[1-9][0-9]?", attrs.get(key, "")):
                    node.set(key, attrs[key])
    return html.tostring(root, encoding="unicode")


def plain_text(markup: str) -> str:
    root = html.fragment_fromstring(markup or "<p></p>", create_parent="div")
    return " ".join(" ".join(root.itertext()).split())


def content_fields(markup: str, url: str, source: str) -> dict:
    cleaned = sanitize_content(markup, url)
    text = plain_text(cleaned)
    return {
        "content_html": cleaned,
        "content_text": text,
        "content_source": source,
        "content_fetched_at": datetime.utcnow() if source != "summary" and text else None,
    }


def extract_page(markup: str, url: str) -> str:
    tree = html.fromstring(markup)
    if urlsplit(url).hostname == "aihot.news":
        articles = tree.xpath("//article")
        if articles:
            article = articles[0]
            parts = []
            for section in article.xpath("./section"):
                prose = section.xpath('.//div[contains(concat(" ", normalize-space(@class), " "), " prose ")]')
                if prose:
                    parts.append("<h2>正文</h2>" + html.tostring(prose[0], encoding="unicode"))
                elif section.xpath("./p"):
                    label = section.xpath("string(./div[1])").strip()
                    parts.append(f"<h2>{escape(label or '导读')}</h2>")
                    parts.extend(html.tostring(p, encoding="unicode") for p in section.xpath("./p"))
            parts.extend(html.tostring(node, encoding="unicode") for node in article.xpath("./figure | .//img | ./p"))
            if parts:
                return "".join(parts)
    if urlsplit(url).hostname == "daily.juya.uk":
        main = tree.xpath('//main[@id="main-content"]')
        if main:
            for node in main[0].xpath('.//section[contains(@class, "video-player")]'):
                node.drop_tree()
            return html.tostring(main[0], encoding="unicode")
    text = trafilatura.extract(markup, include_comments=False, include_tables=True) or ""
    return "".join(f"<p>{escape(line)}</p>" for line in text.splitlines() if line.strip())


async def collect_article_content(article: dict) -> dict:
    """Use full RSS content first, otherwise fetch the article page once at ingestion."""
    if article.get("content_text") and article.get("content_source") == "feed":
        return {key: article[key] for key in (
            "content_html", "content_text", "content_source", "content_fetched_at"
        )}
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            response = await client.get(article["url"], headers={"User-Agent": "AITracker/1.0"})
            response.raise_for_status()
        fields = content_fields(extract_page(response.text, article["url"]), article["url"], "page")
        if fields["content_text"]:
            return fields
    except Exception as exc:
        logger.warning("Body collection failed for %s: %s", article["url"], exc)
    summary = article.get("raw_content") or article.get("summary_zh") or ""
    return content_fields(f"<p>{escape(summary)}</p>", article["url"], "summary")
