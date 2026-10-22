"""database.py — v0.2: registers all models including pending."""
import os
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_PATH = os.getenv("DB_PATH", "data/tracker.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from backend.models import article   # noqa: F401
    from backend.models import model_db  # noqa: F401
    from backend.models import digest    # noqa: F401
    from backend.models import pending   # noqa: F401
    from backend.models import insight   # noqa: F401
    from backend.models import model_task  # noqa: F401
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    # create_all does not add columns to an existing SQLite database.
    with engine.begin() as connection:
        for table, additions in {
            "articles": {"content_html": "TEXT", "content_text": "TEXT",
                         "content_source": "VARCHAR(20)", "content_fetched_at": "DATETIME"},
            "pending_models": {"source_insight_id": "INTEGER", "evidence": "TEXT",
                               "review_decision": "VARCHAR(20)", "review_reason": "TEXT",
                               "reviewer": "VARCHAR(30)", "reviewed_at": "DATETIME", "model_id": "INTEGER"},
            "models": {"source_url": "TEXT", "release_precision": "VARCHAR(10)",
                       "existence_status": "VARCHAR(20)", "weight_access": "VARCHAR(20)",
                       "api_availability": "VARCHAR(20)", "announced_date": "DATE", "available_date": "DATE",
                       "field_states": "TEXT", "last_checked_at": "DATETIME", "next_review_at": "DATETIME"},
            "insights": {"body_paragraphs": "TEXT"},
        }.items():
            columns = {column["name"] for column in inspect(connection).get_columns(table)}
            for name, kind in additions.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {kind}"))
