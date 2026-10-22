import re
import unittest

from fastapi.testclient import TestClient

from backend.main import app


class FrontendCacheTests(unittest.TestCase):
    def test_reader_assets_revalidate_and_script_url_changes_from_legacy(self):
        # No lifespan: asset checks must not start collection jobs.
        client = TestClient(app)
        try:
            for path in ("/", "/reader"):
                page = client.get(path)
                self.assertEqual(page.headers["cache-control"], "no-cache")
                script_url = re.search(r'<script src="(/static/insights\.js\?[^\"]+)"', page.text).group(1)
                script = client.get(script_url)
                self.assertEqual(script.status_code, 200)
                self.assertEqual(script.headers["cache-control"], "no-cache")
                self.assertIn("insight-prose", script.text)
                self.assertNotIn("关键事实", script.text)
                cached = client.get(script_url, headers={"If-None-Match": script.headers["etag"]})
                self.assertEqual(cached.status_code, 304)
                self.assertEqual(cached.headers["cache-control"], "no-cache")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
