from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

import requests

from londo.models import Event

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    source_name: str

    def __init__(self, rate_limit: float = 1.0):
        self.rate_limit = rate_limit
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "Londo Event Aggregator/0.1 (personal project)"}
        )
        self._last_request_time: float = 0.0

    def get(self, url: str) -> requests.Response:
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit:
            delay = self.rate_limit - elapsed
            logger.debug("Rate limit: sleeping %.2fs", delay)
            time.sleep(delay)

        logger.debug("GET %s", url)
        response = self.session.get(url, timeout=30)
        self._last_request_time = time.time()
        response.raise_for_status()
        return response

    @abstractmethod
    def scrape(self) -> list[Event]: ...
