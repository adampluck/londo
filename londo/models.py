from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class PriceTier(BaseModel):
    name: str
    amount: Decimal
    currency: str = "GBP"
    availability: str | None = None


class Location(BaseModel):
    venue_name: str | None = None
    address: str
    city: str | None = None
    country: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class Organizer(BaseModel):
    name: str
    url: str | None = None
    email: str | None = None


class Event(BaseModel):
    source: str
    source_id: str
    source_url: str
    # Shared identity across sources (e.g. "luma:<slug>" when a Newspeak
    # event registers via Luma) — used for cross-source deduplication.
    external_ref: str | None = None
    dedupe_key: str | None = None
    duplicate_of: str | None = None  # "source:source_id" of the canonical event

    title: str
    description: str | None = None
    short_description: str | None = None

    start_datetime: datetime | None = None
    end_datetime: datetime | None = None
    start_date: date | None = None
    is_all_day: bool = False

    location: Location | None = None
    is_online: bool = False

    image_url: str | None = None
    tags: list[str] = Field(default_factory=list)

    price_tiers: list[PriceTier] = Field(default_factory=list)
    is_free: bool = False

    organizer: Organizer | None = None
    age_restriction: str | None = None

    # Enrichment: LLM classification (category/traits/hook/quality) and
    # deterministic area pass — see londo/enrich.py.
    category: str | None = None  # move | connect | expand | think | make
    topics: list[str] = Field(default_factory=list)  # subject/scene labels
    traits: list[str] = Field(default_factory=list)
    hook: str | None = None
    quality_score: int | None = None
    area: str | None = None  # central | east | north | south | west
    enriched_at: datetime | None = None

    scraped_at: datetime
