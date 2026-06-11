from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from londo.models import Event

logger = logging.getLogger(__name__)


class EventEncoder(json.JSONEncoder):
    def default(self, obj: object) -> object:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, date):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def write_events(events: list[Event], source: str, output_dir: str) -> Path:
    dir_path = Path(output_dir)
    dir_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{source}_{timestamp}.json"
    filepath = dir_path / filename

    payload = {
        "source": source,
        "scraped_at": datetime.now().isoformat(),
        "event_count": len(events),
        "events": [e.model_dump() for e in events],
    }

    filepath.write_text(json.dumps(payload, cls=EventEncoder, indent=2))
    logger.info("Wrote %d events to %s", len(events), filepath)
    return filepath
