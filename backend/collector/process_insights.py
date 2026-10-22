"""Process archived materials: python -m backend.collector.process_insights."""
import asyncio
import logging

from dotenv import load_dotenv

load_dotenv()

from backend.collector.insight_agent import build_insights
from backend.database import init_db

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    asyncio.run(build_insights())
