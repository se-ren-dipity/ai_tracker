import json
import unittest
import tempfile
from pathlib import Path
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import database
from backend.collector import feeds, insight_agent, scheduler, source_fetch
from backend.collector.content import collect_article_content
from backend.collector.source_items import prepare_source_items
from backend.models.article import Article
from backend.models.insight import Insight, SourceItem


class FeedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        setting = patch.object(source_fetch, "cache_dir", return_value=Path(directory.name))
        setting.start()
        self.addCleanup(setting.stop)

    def test_wechat_identity_ignores_tracking_but_preserves_subarticles(self):
        original = "https://mp.weixin.qq.com/s?__biz=Publisher&mid=123&idx=1&sn=signature"
        variant = "http://mp.weixin.qq.com/s?scene=1&idx=1&mid=123&__biz=Publisher&chksm=changed#read"
        self.assertEqual(feeds.article_key(original), feeds.article_key(variant))
        for other in (original.replace("idx=1", "idx=2"), original.replace("mid=123", "mid=124"),
                      original.replace("Publisher", "OtherPublisher"), "https://mp.weixin.qq.com/s/short-link"):
            self.assertNotEqual(feeds.article_key(original), feeds.article_key(other))
        self.assertNotEqual(feeds.article_key("https://example.com/article?id=1"),
                            feeds.article_key("https://example.com/article?id=2"))

    async def test_wechat_feed_without_guid_retains_body_images_and_date(self):
        body = "完整原文。" * 1500 + "末尾的新增技术细节。"
        xml = f'''<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
        <channel><title>机器之心</title><item><title>工程更新</title>
        <link>https://mp.weixin.qq.com/s?__biz=Publisher&amp;mid=123&amp;idx=1</link>
        <pubDate>Mon, 26 Oct 2026 10:00:00 +0800</pubDate><description>简短摘要</description>
        <content:encoded><![CDATA[<p>{body}</p><img src="https://images.example.com/figure.jpg"/>]]></content:encoded>
        </item></channel></rss>'''
        client = AsyncMock()
        client.get.return_value = httpx.Response(200, content=xml.encode(), request=httpx.Request("GET", "https://example.com/feed"))
        with patch.object(source_fetch.httpx, "AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            articles = await feeds.fetch_feed("机器之心", "https://example.com/feed")
            content = await collect_article_content(articles[0])
            client.get.assert_awaited_once()
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["published"], datetime(2026, 10, 26, 2))
        self.assertEqual(articles[0]["raw_content"], "简短摘要")
        self.assertIn(body, content["content_text"])
        self.assertIn("https://images.example.com/figure.jpg", content["content_html"])
        self.assertEqual(content["content_source"], "feed")

    async def test_feed_failure_logs_source_and_status_without_credentials(self):
        url = "https://example.com/rss?token=private-value"
        client = AsyncMock()
        client.get.return_value = httpx.Response(429, request=httpx.Request("GET", url))
        with patch.object(source_fetch.httpx, "AsyncClient") as factory, patch.object(source_fetch.asyncio, "sleep", AsyncMock()), self.assertLogs(feeds.logger, "WARNING") as logs:
            factory.return_value.__aenter__.return_value = client
            self.assertEqual(await feeds.fetch_feed("Test Source", url), [])
        self.assertIn("Test Source: 429", " ".join(logs.output))
        self.assertNotIn("private-value", " ".join(logs.output))

    def test_config_adds_only_the_two_selected_wechat_sources(self):
        sources = feeds.load_sources()
        added = {source["name"]: source["url"] for source in sources if source["name"] in {"新智元", "机器之心"}}
        self.assertEqual(len(added), 2)
        self.assertTrue(all(url.startswith("https://wechat2rss.bestblogs.dev/feed/") for url in added.values()))
        self.assertNotIn("量子位", {source["name"] for source in sources})


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        database.Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, autoflush=False)

    def tearDown(self):
        self.engine.dispose()

    async def test_all_sources_are_archived_beyond_thirty_and_repeats_do_not_duplicate(self):
        existing = "https://mp.weixin.qq.com/s?__biz=Publisher&mid=1&idx=1&sn=original"
        with self.session() as db:
            db.add(Article(url=existing, title="Existing", source="机器之心"))
            db.commit()
        sources = [{"name": name, "url": "https://example.com/" + name} for name in ("First", "Unavailable", "Last")]
        first = [{"url": f"https://example.com/article/{i}", "title": f"Article {i}", "source": "First", "raw_content": "Body"} for i in range(35)]
        urls = [existing + "&scene=2", existing.replace("mid=1", "mid=2"),
                existing.replace("mid=1", "mid=2") + "&scene=3", existing.replace("mid=1", "mid=2").replace("idx=1", "idx=2")]
        last = [{"url": url, "title": "WeChat", "source": "Last", "raw_content": "Body"} for url in urls]
        async def fetch(name, url):
            return {"First": first, "Unavailable": [], "Last": last}[name]
        with patch.object(scheduler, "SessionLocal", self.session), patch.object(scheduler, "load_sources", return_value=sources), \
                patch.object(scheduler, "fetch_feed", AsyncMock(side_effect=fetch)) as fetched, \
                patch.object(scheduler, "collect_article_content", AsyncMock(return_value={"content_html": "<p>Body</p>"})) as body, \
                patch.object(scheduler, "build_insights", AsyncMock()) as analyze:
            await scheduler.collect_news()
            await scheduler.collect_news()
        self.assertEqual(fetched.await_count, 6)
        self.assertEqual(body.await_count, 37)
        self.assertEqual(analyze.await_count, 2)
        with self.session() as db:
            self.assertEqual(db.query(Article).count(), 38)
            self.assertEqual(db.query(Article).filter_by(source="Last").count(), 2)
            self.assertEqual(db.get(Article, 1).url, existing)

    def test_cross_source_merge_keeps_new_evidence_and_deduplicates_exact_facts(self):
        with self.session() as db:
            for index, source in enumerate(("机器之心", "新智元"), 1):
                db.add(Article(id=index, url=f"https://example.com/{index}", title="同事件的不同标题", source=source,
                               content_html="<p>共同事实。指标为 60。后续更正指标为 70。</p>"))
            db.commit()
            prepare_source_items(db)
            for identifier, score in ((1, 60), (2, 70)):
                value = insight_agent.Analysis(item_id=identifier, keep=True, event_key="example-v2-benchmark", event_kind="evaluation",
                    title="Example 评测", category="engineering", summary="评测更新", analysis="可能影响选型", next_step="核实测试条件",
                    facts=[{"text": "共同事实", "quote": "共同事实。"}, {"text": "模型得分变化", "quote": f"指标为 {score}"}])
                insight_agent.save_analysis(db, value)
            db.commit()
            event = db.query(Insight).one()
            facts = json.loads(event.facts)
            self.assertEqual(len(facts), 3)
            self.assertEqual({fact["quote"] for fact in facts}, {"共同事实。", "指标为 60", "指标为 70"})
            self.assertEqual(db.query(SourceItem).filter_by(insight_id=event.id).count(), 2)
            self.assertEqual({fact["source_item_id"] for fact in facts}, {1, 2})


if __name__ == "__main__":
    unittest.main()
