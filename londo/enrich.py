from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from londo.geo import assign_area
from londo.models import Event

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"

# Intent categories — the frontend's primary navigation.
CATEGORIES = ("move", "connect", "expand", "think", "make")

# Subject/scene labels — what the event is *about*, independent of form.
# Also powers the tech / non-tech lens (see web/app.js TECH_TOPICS).
TOPIC_VOCAB = (
    "psychedelics", "consciousness", "connection & intimacy", "tech & ai",
    "startups & work", "arts & creativity", "music & sound",
    "nature & outdoors", "healing & wellbeing", "spirituality & ritual",
    "society & politics", "science & ideas",
)

TRAIT_VOCAB = (
    "beginner-friendly", "sober", "outdoors", "touch-based", "talky",
    "embodied", "ceremony", "music", "workshop", "late-night",
    "daytime", "small-group",
)

SYSTEM_PROMPT = f"""\
You classify and editorialize London events for Londo, a hub for in-person
events that connect and inspire people — from ecstatic dance to AI salons,
breathwork to philosophy nights.

Assign exactly one category:
- move: dance (ecstatic, 5Rhythms, contact improv), movement, running, yoga, physical practice
- connect: authentic relating, speed-friending, circles, suppers, socials, community gatherings
- expand: breathwork, meditation, psychedelics, sound baths, ceremony, altered states, spirituality
- think: AI, tech, civic tech, philosophy, science, talks, salons, book clubs, politics
- make: hands-on workshops, crafts, singing, music-making, cooking, creative practice

Pick the category matching the PRIMARY activity a visitor would do there.

Topics: pick 1-3 that describe what the event is ABOUT (its subject and
scene, not its format), only from: {", ".join(TOPIC_VOCAB)}.
A founders' dinner is "startups & work"; a philosophy salon is
"science & ideas"; an ecstatic dance is "music & sound" and maybe
"spirituality & ritual" — never force a topic that doesn't clearly fit.

Traits: pick 0-4 that clearly apply, only from: {", ".join(TRAIT_VOCAB)}.

Hook: one vivid sentence (max 110 characters) selling why to go. Concrete and
specific to this event — name the person, practice, or idea that makes it
worth leaving the house. No emoji, no exclamation marks, no "join us".

Quality score 0-100: how complete and compelling the listing is (clear
description, real venue, named organizer/facilitator, specific programme).
A thin or vague listing scores under 40; a rich specific one scores over 70.

Area: which part of London the venue is in (central, east, north, south,
west), judged from the venue name and address. null if you can't tell.\
"""


class Enrichment(BaseModel):
    category: Literal["move", "connect", "expand", "think", "make"]
    topics: list[str] = Field(default_factory=list)
    traits: list[str] = Field(default_factory=list)
    hook: str
    quality_score: int
    area: Literal["central", "east", "north", "south", "west"] | None = None


def enrich_events(
    events: list[Event], existing: dict[tuple[str, str], dict] | None = None
) -> int:
    """Assign area (deterministic) to all events, and category/traits/hook/
    quality (LLM) to canonical events that don't already have them.

    `existing` maps (source, source_id) -> previously stored enrichment row;
    matching events reuse it instead of a new API call. Returns the number
    of LLM calls made. Requires ANTHROPIC_API_KEY; without it only the
    deterministic area pass runs.
    """
    existing = existing or {}
    now = datetime.now(timezone.utc)

    for event in events:
        if event.area is None:
            event.area = assign_area(event)

    have_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not have_key:
        logger.warning("ANTHROPIC_API_KEY not set - skipping LLM enrichment")

    client = None
    calls = 0
    for event in events:
        if event.duplicate_of:
            continue  # hidden in the UI; don't spend tokens on it
        prior = existing.get((event.source, event.source_id))
        # topics arrived later than the other fields: their absence forces
        # one re-enrichment of older rows, then reuse resumes
        if (
            prior
            and prior.get("category")
            and prior.get("hook")
            and prior.get("topics")
        ):
            event.category = prior["category"]
            event.topics = prior["topics"]
            event.traits = prior.get("traits") or []
            event.hook = prior["hook"]
            event.quality_score = prior.get("quality_score")
            if event.area is None:
                event.area = prior.get("area")
            event.enriched_at = _parse_dt(prior.get("enriched_at")) or now
            continue
        if not have_key:
            continue

        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        try:
            enrichment = _classify(client, event)
        except Exception:
            logger.exception("Enrichment failed for %s", event.title)
            continue
        calls += 1
        event.category = enrichment.category
        event.topics = [t for t in enrichment.topics if t in TOPIC_VOCAB][:3]
        event.traits = [t for t in enrichment.traits if t in TRAIT_VOCAB]
        event.hook = enrichment.hook[:140].strip()
        event.quality_score = max(0, min(100, enrichment.quality_score))
        if event.area is None:
            event.area = enrichment.area  # postcode/geo pass came up empty
        event.enriched_at = now

    if calls:
        logger.info("Enriched %d events via %s", calls, MODEL)
    return calls


def _classify(client, event: Event) -> Enrichment:
    response = client.messages.parse(
        model=MODEL,
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _event_brief(event)}],
        output_format=Enrichment,
    )
    return response.parsed_output


def _event_brief(event: Event) -> str:
    loc = event.location
    price = "free" if event.is_free else (
        ", ".join(f"£{t.amount}" for t in event.price_tiers[:4]) or "unknown"
    )
    parts = [
        f"Title: {event.title}",
        f"Tags: {', '.join(event.tags[:10]) or 'none'}",
        f"Venue: {(loc.venue_name or loc.address) if loc else 'unknown'}",
        f"Organizer: {event.organizer.name if event.organizer else 'unknown'}",
        f"Price: {price}",
        f"Description: {(event.description or '')[:1500]}",
    ]
    return "\n".join(parts)


def _parse_dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
