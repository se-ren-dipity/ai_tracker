import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lxml import html
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import database
from backend.collector.content import collect_article_content, content_fields, extract_page, sanitize_content
from backend.collector.feeds import fetch_feed
from backend.models.article import Article
from backend.routers.articles import router


class ContentTests(unittest.IsolatedAsyncioTestCase):
    def test_sanitizer_preserves_structure_and_removes_active_content(self):
        markup = '''<h1>Source title</h1><h2 id="topic" style="color:red">Topic</h2>
        <p onclick="attack()">Text <strong>bold</strong><script>attack()</script>
        <a href="javascript:attack()">bad link</a><a href="/paper">paper</a>
        <a href="#topic">jump</a><img src="/figure.png" onerror="attack()"></p>
        <iframe src="https://example.com"></iframe><!-- source comment -->
        <ul><li>Item</li></ul><table><tr><td colspan="2">Value</td></tr></table>'''
        result = sanitize_content(markup, "https://example.com/news/1")
        tree = html.fromstring(result)
        self.assertFalse(tree.xpath("//script | //iframe | //h1 | //@style | //@onclick | //@onerror"))
        self.assertNotIn("javascript:", result)
        self.assertEqual(tree.xpath("//h2/@id"), ["article-section-1"])
        self.assertIn("#article-section-1", tree.xpath("//a/@href"))
        self.assertIn("https://example.com/paper", tree.xpath("//a/@href"))
        self.assertEqual(tree.xpath("//img/@src"), ["https://example.com/figure.png"])
        self.assertEqual(tree.xpath("//li/text()"), ["Item"])
        self.assertEqual(tree.xpath("//td/@colspan"), ["2"])

    async def test_full_feed_content_is_saved_beyond_summary_limit(self):
        body = "Full article " * 600 + "FINAL_SECTION"
        xml = f'''<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
        <channel><title>Feed</title><item><title>News</title><link>https://example.com/1</link>
        <description>Short summary</description>
        <content:encoded><![CDATA[<h2>Section</h2><p>{body}</p>]]></content:encoded>
        </item></channel></rss>'''
        client = AsyncMock()
        client.get.return_value = httpx.Response(200, text=xml, request=httpx.Request("GET", "https://example.com/rss"))
        with patch("backend.collector.feeds.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            articles = await fetch_feed("Feed", "https://example.com/rss")
        self.assertEqual(articles[0]["raw_content"], "Short summary")
        self.assertIn("FINAL_SECTION", articles[0]["content_text"])
        with patch("backend.collector.content.httpx.AsyncClient") as factory:
            result = await collect_article_content(articles[0])
            factory.assert_not_called()
        self.assertEqual(result["content_source"], "feed")

    def test_image_proxy_is_replaced_with_nonexpiring_source_url(self):
        markup = '<img src="/api/img-proxy?u=https%3A%2F%2Fimages.example.com%2Fchart.png&amp;exp=1&amp;sig=old">'
        result = sanitize_content(markup, "https://aihot.news/items/1")
        self.assertEqual(html.fromstring(result).xpath("//img/@src"), ["https://images.example.com/chart.png"])

    async def test_fetch_failure_keeps_readable_summary_without_claiming_full_body(self):
        client = AsyncMock()
        client.get.side_effect = httpx.ConnectError("Offline")
        with patch("backend.collector.content.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            result = await collect_article_content({"url": "https://example.com/1", "raw_content": "Use <model> & tools"})
        self.assertEqual(result["content_source"], "summary")
        self.assertIsNone(result["content_fetched_at"])
        self.assertEqual(result["content_text"], "Use <model> & tools")
        self.assertNotIn("<model>", result["content_html"])

    def test_aihot_extracts_body_and_quotes_without_navigation(self):
        markup = '''<article><div>Author metadata</div>
        <section><div>AI 导读</div><p>Introduction</p></section>
        <section><nav>Translation controls</nav><div class="prose"><p>Full body</p><ul><li>Fact</li></ul></div></section>
        <figure><blockquote>Quoted text</blockquote></figure><div>Related stories</div></article>'''
        result = content_fields(extract_page(markup, "https://aihot.news/items/1"), "https://aihot.news/items/1", "page")
        self.assertIn("Full body", result["content_text"])
        self.assertIn("Quoted text", result["content_text"])
        self.assertNotIn("Translation controls", result["content_text"])
        self.assertNotIn("Related stories", result["content_text"])


class ReaderApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Article.__table__.create(self.engine)
        self.session = sessionmaker(bind=self.engine)
        with self.session() as db:
            db.add(Article(id=1, url="https://example.com/1", title="Saved news", source="Feed",
                           raw_content="Brief", summary_zh="Brief", created_at=datetime.now(),
                           **content_fields("<h2>Details</h2><p>Unique body search phrase</p>", "https://example.com/1", "feed")))
            db.add(Article(id=2, url="https://example.com/2", title="Legacy news", raw_content="Legacy summary"))
            db.commit()
        app = FastAPI()
        app.include_router(router)

        def get_db():
            with self.session() as db:
                yield db

        app.dependency_overrides[database.get_db] = get_db
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_detail_reads_saved_body_without_fetching_source(self):
        with patch("backend.collector.content.httpx.AsyncClient", side_effect=AssertionError("Must not fetch")):
            response = self.client.get("/api/articles/1")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["has_content"])
        self.assertIn("Unique body search phrase", response.json()["content_html"])
        self.assertNotIn("content_html", self.client.get("/api/articles").json()["items"][0])

    def test_full_body_search_and_existing_static_routes(self):
        result = self.client.get("/api/articles", params={"q": "Unique body search"}).json()
        self.assertEqual([item["id"] for item in result["items"]], [1])
        self.assertEqual(self.client.get("/api/articles/sources").status_code, 200)
        self.assertEqual(self.client.get("/api/articles/today").status_code, 200)

    def test_missing_article_and_legacy_summary(self):
        self.assertEqual(self.client.get("/api/articles/999").status_code, 404)
        legacy = self.client.get("/api/articles/2").json()
        self.assertFalse(legacy["has_content"])
        self.assertEqual(legacy["raw_content"], "Legacy summary")


class MigrationTests(unittest.TestCase):
    def test_existing_rows_survive_repeatable_column_migration(self):
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT)"))
            connection.execute(text("INSERT INTO articles (id, title) VALUES (7, 'Existing article')"))
            connection.execute(text("CREATE TABLE insights (id INTEGER PRIMARY KEY, title TEXT)"))
            connection.execute(text("INSERT INTO insights (id, title) VALUES (4, 'Existing insight')"))
            connection.execute(text("CREATE TABLE models (id INTEGER PRIMARY KEY, name TEXT)"))
            connection.execute(text("INSERT INTO models (id, name) VALUES (1, 'Existing model')"))
            connection.execute(text("CREATE TABLE pending_models (id INTEGER PRIMARY KEY, status TEXT)"))
            connection.execute(text("INSERT INTO pending_models (id, status) VALUES (1, 'draft')"))
        with patch.object(database, "engine", engine):
            database.init_db()
            database.init_db()
        columns = {column["name"] for column in inspect(engine).get_columns("articles")}
        self.assertTrue({"content_html", "content_text", "content_source", "content_fetched_at"} <= columns)
        self.assertIn("body_paragraphs", {column["name"] for column in inspect(engine).get_columns("insights")})
        self.assertIn("review_reason", {column["name"] for column in inspect(engine).get_columns("pending_models")})
        self.assertIn("release_precision", {column["name"] for column in inspect(engine).get_columns("models")})
        with engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT title FROM articles WHERE id=7")).scalar(), "Existing article")
            self.assertEqual(connection.execute(text("SELECT title FROM insights WHERE id=4")).scalar(), "Existing insight")
            self.assertEqual(connection.execute(text("SELECT name FROM models WHERE id=1")).scalar(), "Existing model")
            self.assertEqual(connection.execute(text("SELECT status FROM pending_models WHERE id=1")).scalar(), "draft")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
