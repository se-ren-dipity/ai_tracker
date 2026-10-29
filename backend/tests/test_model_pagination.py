import unittest
from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.database import get_db
from backend.models.model_db import AIModel
from backend.routers.models import router


class ModelPaginationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        AIModel.__table__.create(self.engine)
        self.db = Session(self.engine)
        self.db.add_all([
            AIModel(name=f"Model {i:02}", type="LLM" if i % 2 else "VLM",
                    organization=f"Lab {i % 3}", license=f"License {i % 2}",
                    params_b=i, context_length=i * 1000,
                    release_date=date(2026, 10, 29) if i < 41 else None,
                    notes="完整描述" * 100)
            for i in range(1, 43)
        ])
        self.db.commit()
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def test_pages_cover_every_model_once_with_stable_ties_and_null_dates_last(self):
        pages = [self.client.get("/api/models", params={"page": page, "page_size": 20}).json()
                 for page in range(1, 4)]
        self.assertEqual([len(page["items"]) for page in pages], [20, 20, 2])
        ids = [model["id"] for page in pages for model in page["items"]]
        self.assertEqual(len(set(ids)), 42)
        self.assertEqual(ids[:40], list(range(40, 0, -1)))
        self.assertEqual(ids[-2:], [42, 41])
        self.assertEqual(pages[0]["total"], 42)
        self.assertEqual(pages[0]["items"][0]["notes"], "完整描述" * 100)

    def test_filters_apply_before_pagination_and_count(self):
        result = self.client.get("/api/models", params={"type": "LLM", "q": "Model", "page": 2, "page_size": 20}).json()
        self.assertEqual(result["total"], 21)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["type"], "LLM")
        empty = self.client.get("/api/models", params={"q": "does not exist"}).json()
        self.assertEqual((empty["total"], empty["items"]), (0, []))

    def test_each_visible_sort_header_orders_the_results(self):
        for field in ["name", "type", "organization", "license", "params_b", "context_length"]:
            with self.subTest(field=field):
                result = self.client.get("/api/models", params={"sort": field, "page_size": 100}).json()
                values = [model[field] for model in result["items"]]
                self.assertEqual(values, sorted(values, reverse=field in {"params_b", "context_length"}))


if __name__ == "__main__":
    unittest.main()
