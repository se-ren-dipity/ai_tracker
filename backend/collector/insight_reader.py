"""Reading assets from the archived section belonging to an event."""
import re
from urllib.parse import urlsplit

from lxml import html

from backend.models.article import Article
from backend.models.insight import SourceItem


def web_url(value):
    try:
        parsed = urlsplit(value or "")
        return parsed.scheme in {"https", "http"} and bool(parsed.hostname) and not parsed.username
    except ValueError:
        return False


def section_assets(article, item):
    root = html.fromstring(article.content_html or "<div></div>")
    if item.section_id:
        headings = root.xpath("//*[@id=$section]", section=item.section_id)
        if not headings:
            return {"images": [], "links": []}
        nodes = [headings[0]]
        for node in headings[0].itersiblings():
            if node.tag in {"h2", "h3"}:
                break
            nodes.append(node)
        root = html.fragment_fromstring("".join(html.tostring(node, encoding="unicode") for node in nodes), create_parent="div")
    elif article.source == "橘鸦早报":
        # A missing section must never fall back to another event or the daily cover.
        return {"images": [], "links": []}
    images, links = [], []
    seen_images, seen_links = set(), set()
    for node in root.xpath(".//img"):
        url = node.get("src", "")
        if not web_url(url) or url in seen_images or re.search(r"/cover/|avatar|qrcode|qr-code|favicon", url, re.I):
            continue
        caption = " ".join(node.xpath("ancestor::figure[1]/figcaption//text()")).strip()
        alt = (node.get("alt") or "").strip()
        if re.search(r"二维码|扫码|公众号名片", caption + alt):
            continue
        seen_images.add(url)
        images.append({"url": url, "alt": alt, "caption": caption,
                       "source_item_id": item.id, "source": article.source})
    for node in root.xpath(".//a[@href]"):
        url = node.get("href", "")
        if not web_url(url) or url in seen_links or url == article.url:
            continue
        seen_links.add(url)
        host = urlsplit(url).hostname.removeprefix("www.")
        label = node.text_content().strip()
        if host in {"github.com", "huggingface.co"}:
            label = ("GitHub" if host == "github.com" else "Hugging Face") + " · " + "/".join(urlsplit(url).path.strip("/").split("/")[:2])
        elif not label or label.startswith("http") or len(label) > 32:
            label = host
        links.append({"url": url, "label": label})
    return {"images": images, "links": links}


def reading_assets(db, insight_id):
    rows = db.query(SourceItem, Article).join(Article, SourceItem.article_id == Article.id).filter(
        SourceItem.insight_id == insight_id).order_by(SourceItem.id).all()
    result = {"images": [], "links": []}
    for item, article in rows:
        assets = section_assets(article, item)
        for kind in result:
            seen = {entry["url"] for entry in result[kind]}
            for entry in assets[kind]:
                if entry["url"] not in seen:
                    result[kind].append(entry)
                    seen.add(entry["url"])
    result["images"] = result["images"][:3]
    result["links"].sort(key=lambda link: urlsplit(link["url"]).hostname not in {"github.com", "huggingface.co", "arxiv.org"})
    result["links"] = result["links"][:3]
    return result
