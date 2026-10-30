import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.collector import insight_editor as editor
from backend.collector.insight_agent import save_analysis
from backend.collector.insight_reader import section_assets, reading_assets
from backend.models.article import Article
from backend.models.insight import Insight, SourceItem
from backend.tests.test_insights import result


class ReadingAssetsTests(unittest.TestCase):
    def test_images_and_links_are_scoped_to_the_event(self):
        article = Article(source="橘鸦早报", url="https://daily.test/issue", content_html='''
            <div><img src="https://images.test/cover/day.png">
            <h3 id="article-section-1"><a href="https://github.com/team/engine">Engine #1</a></h3>
            <p>正文</p><figure><img src="https://images.test/chart.png" alt="吞吐图"><figcaption>性能对比</figcaption></figure>
            <p><img src="https://images.test/chart.png"><img src="https://images.test/avatar.png"></p>
            <p><img src="javascript:alert(1)"><img src="https://images.test/ad.png" alt="扫码关注"></p>
            <h3 id="article-section-2">Other #2</h3><img src="https://images.test/other.png"></div>''')
        item = SourceItem(id=1, section_id="article-section-1")
        assets = section_assets(article, item)
        self.assertEqual([img["url"] for img in assets["images"]], ["https://images.test/chart.png"])
        self.assertEqual(assets["images"][0]["caption"], "性能对比")
        self.assertEqual(assets["links"], [{"url": "https://github.com/team/engine", "label": "GitHub · team/engine"}])
        for section in ("missing", None):
            item.section_id = section
            self.assertEqual(section_assets(article, item), {"images": [], "links": []})

    def test_normal_article_keeps_illustrations_but_rejects_unsafe_links(self):
        article = Article(source="AIHOT", url="https://news.test/post", content_html='''
            <div><img src="https://images.test/one.gif"><a href="javascript:alert(1)">坏链接</a>
            <a href="https://news.test/post">原报道</a><a href="https://docs.test/sdk">API 文档</a></div>''')
        assets = section_assets(article, SourceItem(id=9))
        self.assertEqual(len(assets["images"]), 1)
        self.assertEqual(assets["links"], [{"url": "https://docs.test/sdk", "label": "API 文档"}])


class EditorTests(unittest.IsolatedAsyncioTestCase):
    async def test_truncated_article_output_is_not_retried_at_the_same_size(self):
        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="length", message=SimpleNamespace(content='{"items":['))]))
        with patch.object(editor, "get_client", return_value=client), self.assertRaises(editor.OutputTruncated):
            await editor.edit_batch([{"id": 1}])
        self.assertEqual(client.chat.completions.create.await_count, 1)

    async def test_split_articles_save_successes_and_continue_past_one_failed_article(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)
        with session() as db:
            for i in range(1, 4):
                db.add(Insight(id=i, event_key=str(i), title="Title", category="engineering", facts="[]"))
            db.commit()
        calls = []
        async def edit(batch):
            calls.append([item["id"] for item in batch])
            if len(batch) > 1 or batch[0]["id"] == 2:
                raise editor.OutputTruncated("too long")
            return [editor.EditedArticle(id=batch[0]["id"], title="Title", summary="Summary", paragraphs=["First", "Second"])]
        with patch.object(editor, "SessionLocal", session), patch.object(editor, "edit_batch", side_effect=edit):
            with self.assertRaises(ValueError):
                await editor.compose_articles()
        self.assertEqual(calls, [[1, 2, 3], [1], [2, 3], [2], [3]])
        with session() as db:
            self.assertEqual([bool(db.get(Insight, i).body_paragraphs) for i in [1, 2, 3]], [True, False, True])

    async def test_retries_invalid_mapping_and_rejects_generated_html(self):
        client = MagicMock()
        client.with_options.return_value = client
        def response(items):
            items = [{"title": "最新标题", "summary": "最新摘要", **item} for item in items]
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps({"items": items})))])
        client.chat.completions.create = AsyncMock(side_effect=[response([{"id": 2, "paragraphs": ["导语", "正文"]}]), response([{"id": 1, "paragraphs": ["导语", "正文"]}])])
        with patch.object(editor, "get_client", return_value=client):
            articles = await editor.edit_batch([{"id": 1}])
        self.assertEqual(articles[0].paragraphs, ["导语", "正文"])
        self.assertEqual(client.chat.completions.create.await_count, 2)
        bad = response([{"id": 1, "paragraphs": ["导语", "<img src=x>"]}])
        client.chat.completions.create = AsyncMock(return_value=bad)
        with patch.object(editor, "get_client", return_value=client), self.assertRaises(ValueError):
            await editor.edit_batch([{"id": 1}])

    async def test_composition_is_saved_and_new_reports_invalidate_it(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)
        with session() as db:
            for index in (1, 2):
                db.add(Article(id=index, url=f"https://test/{index}", title="Title", source="AIHOT", content_html='<p><img src="https://images.test/shared.png"></p>'))
                db.add(SourceItem(id=index, article_id=index, section_key=str(index), title="Title", body_text="增加流式接口", published=datetime(2026, 10, 21), status="pending"))
            db.commit()
            save_analysis(db, result())
            db.commit()
        output = [editor.EditedArticle(id=1, title="最新标题", summary="最新摘要", paragraphs=["模型增加流式接口。", "可能改善接入体验。"])]
        with patch.object(editor, "SessionLocal", session), patch.object(editor, "edit_batch", new_callable=AsyncMock, return_value=output) as edit:
            await editor.compose_articles()
            await editor.compose_articles()
            edit.assert_awaited_once()
        with session() as db:
            self.assertEqual(json.loads(db.get(Insight, 1).body_paragraphs), output[0].paragraphs)
            self.assertEqual(db.get(Insight, 1).title, "最新标题")
            self.assertEqual(db.get(Insight, 1).summary, "最新摘要")
            save_analysis(db, result(2, duplicate_of=1))
            db.commit()
            self.assertIsNone(db.get(Insight, 1).body_paragraphs)
            self.assertEqual(len(reading_assets(db, 1)["images"]), 1)
        # A failed editing request leaves the original analysis intact and retryable.
        with patch.object(editor, "SessionLocal", session), patch.object(editor, "edit_batch", new_callable=AsyncMock, side_effect=ValueError("invalid")), self.assertRaises(ValueError):
            await editor.compose_articles()
        with session() as db:
            row = db.get(Insight, 1)
            self.assertIsNone(row.body_paragraphs)
            self.assertEqual(row.summary, "新模型增加流式接口。")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
