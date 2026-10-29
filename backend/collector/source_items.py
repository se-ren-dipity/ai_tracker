"""Split daily digests into independently analyzable events."""
import json
import re

from lxml import html

from backend.collector.content import plain_text
from backend.models.article import Article
from backend.models.insight import SourceItem


def split_article(article: dict) -> list[dict]:
    root = html.fromstring(article.get("content_html") or "<div></div>")
    if article.get("source") == "橘鸦早报":
        headings = [node for node in root.xpath(".//h2 | .//h3")
                    if re.search(r"#\d+\s*$", node.text_content())]
        if not headings:
            raise ValueError("日报中未找到独立新闻章节，保留原文等待处理")
        parts = []
        for heading in headings:
            nodes = []
            for node in heading.itersiblings():
                if node.tag in {"h2", "h3"}:
                    break
                nodes.append(html.tostring(node, encoding="unicode"))
            title = re.sub(r"\s*#\d+\s*$", "", heading.text_content()).strip()
            sections = heading.xpath("preceding-sibling::h2[1]")
            section = sections[0].text_content().strip() if sections else ""
            parts.append((heading.get("id"), title, "".join(nodes), section))
    else:
        parts = [(None, article["title"], html.tostring(root, encoding="unicode"), "")]
    result = []
    for section_id, title, markup, section in parts:
        text = plain_text(markup) or article.get("raw_content") or ""
        tree = html.fragment_fromstring(markup or "<div></div>", create_parent="div")
        links = list(dict.fromkeys(link for link in tree.xpath(".//a/@href") if link.startswith("https://")))
        result.append({
            "article_id": article["id"], "section_key": f"{article['id']}:{section_id or 'body'}",
            "section_id": section_id, "source_section": section, "title": title, "body_text": text,
            "links": json.dumps(links, ensure_ascii=False), "published": article["published"],
        })
    return result


def prepare_source_items(db):
    existing = {row.section_key: row for row in db.query(SourceItem).all()}
    errors = []
    for article in db.query(Article).all():
        data = article.to_dict(include_content=True)
        data["published"] = article.published or article.created_at
        try:
            for item in split_article(data):
                if item["section_key"] not in existing:
                    row = SourceItem(**item)
                    db.add(row)
                    existing[item["section_key"]] = row
                else:
                    existing[item["section_key"]].source_section = item["source_section"]
        except ValueError as exc:
            errors.append(f"{article.title}: {exc}")
    db.commit()
    return errors
