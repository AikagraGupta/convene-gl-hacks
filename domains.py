#!/usr/bin/env python3
"""domains.py — what kind of thing are we booking?

RainCheck v1 hard-coded one answer: a restaurant table. That assumption is
spread across four files -- the Overpass query in places.py, the constraint
and picks prompts in pipeline.py, the dynamic variables the voice agent
speaks, and the words the bot uses in the group chat. Generalising means
naming that assumption once and passing it around, not find-and-replacing
"restaurant" with "venue".

A Category is the whole difference between two bookings. If something differs
between booking a table and booking a pickleball court, it belongs here.

Three things worth knowing before trusting any of this:

1. THE OSM SELECTORS BELOW ARE UNVERIFIED for every category except
   `restaurant`. Restaurant coverage was measured: 1,199 named places across
   HK Island north and Kowloon, 156 with a dialable number. Nobody has
   measured the others. Run `python3 probe_coverage.py` on a machine with
   working network before building a demo on any of them.

2. COVERAGE IS THE WHOLE RISK. A category where OSM knows ten venues and two
   phone numbers cannot carry a demo, however good the agent is. Party rooms
   are the specific worry: in Hong Kong they are small businesses on the
   fifteenth floor of industrial buildings, which is exactly the kind of place
   OSM maps badly.

3. `books_by` is a claim about the real world, not a config flag. Sports courts
   in Hong Kong are mostly LCSD facilities booked through SmartPLAY, not by
   phoning the pitch, and a voice agent pointed at them would be theatre. Where
   that is true the category says so, and the honest output is a booking link
   rather than a call.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Category:
    key: str
    label: str                     # singular, as spoken: "restaurant"
    plural: str                    # "restaurants"
    verb: str                      # what the group is deciding to do
    osm_selectors: list[str]       # Overpass tag filters, without the bbox
    descriptor_tag: str            # the OSM tag that distinguishes venues
    descriptor_label: str          # what to call it in chat: "cuisine", "sport"
    constraint_hints: list[str]    # what to look for in the chat history
    booking_fields: list[str]      # what the caller must settle on the phone
    books_by: str                  # "phone" | "platform" | "mixed"
    coverage_verified: bool        # has probe_coverage.py been run for this?
    notes: str = ""


# --- the one we measured -------------------------------------------------

RESTAURANT = Category(
    key="restaurant",
    label="restaurant",
    plural="restaurants",
    verb="eat",
    osm_selectors=['["amenity"~"^(restaurant|fast_food)$"]["name"]'],
    descriptor_tag="cuisine",
    descriptor_label="cuisine",
    constraint_hints=[
        "dietary restrictions and allergies, with the quote they came from",
        "cuisines vetoed or repeated recently",
        "districts people are travelling from",
        "budget per head",
        "party size and time",
    ],
    booking_fields=["party_size", "when_text", "booking_name", "dietary_notes"],
    books_by="phone",
    coverage_verified=True,
    notes="1,199 named, 156 callable across HK Island north + Kowloon.",
)

# --- the new ones, all unmeasured ---------------------------------------

PARTY_ROOM = Category(
    key="party_room",
    label="party room",
    plural="party rooms",
    verb="celebrate",
    # Hong Kong party rooms have no settled OSM tagging. events_venue is the
    # closest standard tag; the others are guesses that the probe will confirm
    # or kill. Expect this to be the weakest category by a distance.
    osm_selectors=[
        '["amenity"="events_venue"]["name"]',
        '["leisure"="party_room"]["name"]',
        '["shop"="party"]["name"]',
    ],
    descriptor_tag="capacity",
    descriptor_label="capacity",
    constraint_hints=[
        "headcount, which drives everything else",
        "the occasion, since a birthday and a farewell want different rooms",
        "hours needed and whether it runs past midnight",
        "whether they need karaoke, a projector, or a mahjong table",
        "whether outside food and drink is allowed",
        "budget per head",
    ],
    booking_fields=["party_size", "when_text", "duration_hours", "booking_name",
                    "equipment_needed", "outside_food"],
    books_by="phone",
    coverage_verified=False,
    notes=("Highest risk category. HK party rooms sit in industrial buildings "
           "and advertise on Instagram, not OSM. If the probe returns single "
           "digits, this category needs a different data source, not a better "
           "prompt."),
)

COURT_SPORT = Category(
    key="court_sport",
    label="court",
    plural="courts",
    verb="play",
    osm_selectors=[
        '["leisure"="pitch"]["sport"]["name"]',
        '["leisure"="sports_centre"]["name"]',
    ],
    descriptor_tag="sport",
    descriptor_label="sport",
    constraint_hints=[
        "which sport, and how many are playing",
        "indoor or outdoor, which in Hong Kong is a weather and heat question",
        "districts people are travelling from",
        "how long they want the court for",
        "whether anyone needs to borrow equipment",
    ],
    booking_fields=["party_size", "when_text", "duration_hours", "booking_name",
                    "sport", "equipment_needed"],
    books_by="platform",
    coverage_verified=False,
    notes=("Most Hong Kong courts are LCSD public facilities booked through "
           "SmartPLAY, not by phone, and SmartPLAY needs a registered account "
           "and ID. Private clubs and commercial pickleball venues DO take "
           "phone bookings. So this category must split its output: call the "
           "ones that answer phones, hand back a booking link for the rest. "
           "Pretending to phone a government pitch would be the fake step the "
           "whole project exists to avoid."),
)

KARAOKE = Category(
    key="karaoke",
    label="karaoke box",
    plural="karaoke boxes",
    verb="sing",
    osm_selectors=[
        '["amenity"="karaoke_box"]["name"]',
        '["leisure"="karaoke"]["name"]',
    ],
    descriptor_tag="capacity",
    descriptor_label="room size",
    constraint_hints=[
        "headcount and how long",
        "whether they want a package with food and drinks",
        "districts people are travelling from",
        "budget per head",
    ],
    booking_fields=["party_size", "when_text", "duration_hours", "booking_name",
                    "package_wanted"],
    books_by="phone",
    coverage_verified=False,
    notes="Chains (Neway, CEO) are the realistic target and are phone-bookable.",
)

BBQ_SITE = Category(
    key="bbq_site",
    label="barbecue site",
    plural="barbecue sites",
    verb="barbecue",
    osm_selectors=['["amenity"="bbq"]', '["leisure"="bbq"]'],
    descriptor_tag="fuel",
    descriptor_label="type",
    constraint_hints=[
        "headcount",
        "how far people will travel, since most sites are in country parks",
        "whether anyone is bringing a car",
        "time of day and whether it runs into the evening",
    ],
    booking_fields=["party_size", "when_text", "booking_name"],
    books_by="mixed",
    coverage_verified=False,
    notes=("Many OSM bbq nodes are unnamed public grills with no phone and no "
           "booking at all. Expect `name` to be missing far more often than in "
           "any other category, which breaks the poll before it breaks the call."),
)


CATEGORIES: dict[str, Category] = {
    c.key: c for c in (RESTAURANT, PARTY_ROOM, COURT_SPORT, KARAOKE, BBQ_SITE)
}

DEFAULT_CATEGORY = RESTAURANT.key


def get(key: str | None) -> Category:
    """Never raise on an unknown key. A typo should degrade to dinner, which
    is the category we actually measured, not crash a group chat."""
    return CATEGORIES.get((key or "").strip().lower(), RESTAURANT)


def verified_only() -> list[Category]:
    return [c for c in CATEGORIES.values() if c.coverage_verified]


def build_selector_query(category: Category, bbox: tuple[float, float, float, float],
                         limit: int = 400) -> str:
    """Overpass QL for one category over one bounding box.

    Both node and way are emitted for every selector: a restaurant is usually a
    node, a sports pitch is almost always a way, and querying only nodes was
    what made the first court query come back empty.
    """
    south, west, north, east = bbox
    box = f"{south},{west},{north},{east}"
    lines = []
    for sel in category.osm_selectors:
        lines.append(f"  node{sel}({box});")
        lines.append(f"  way{sel}({box});")
    body = "\n".join(lines)
    return f"[out:json][timeout:25];\n(\n{body}\n);\nout center tags {limit};"
