"""Feed model-release events into the automatic admission queue."""
import json
import re

from backend.database import SessionLocal



def model_key(name):
    return re.sub(r"[\W_]+", "", name.casefold().replace("+", "plus"))


def model_lookup(models, table_entries=()):
    """Resolve known aliases, leaving ambiguous same-name versions unlinked."""
    names = {}
    for model in models:
        for name in (model.name, model.full_name):
            if name:
                names.setdefault(model_key(name), set()).add(model.id)
    for entry in table_entries:
        name = json.loads(entry.public_row)["model"]
        names.setdefault(model_key(name), set()).add(entry.model_id)
    return {name: next(iter(ids)) for name, ids in names.items() if len(ids) == 1}


async def queue_model_drafts():
    """News-only admission; paid research is now an explicit admin operation."""
    from backend.collector.model_discovery import reconcile_releases
    await reconcile_releases(session_factory=SessionLocal)
