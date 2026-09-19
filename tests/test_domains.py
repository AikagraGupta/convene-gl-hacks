"""The category catalogue, and what places.py does once it knows the category.

The thing under test here is not clever code, it is a set of claims about the
world: that a pickleball query does not return restaurants, that a category
with no seed list says so instead of improvising, and that one category's
cache cannot poison another's. Each of those was a real way v1's assumptions
could leak into v2.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import domains
import places
from harness import FakeNet, Suite, overpass_ok, url_error


def run() -> Suite:
    s = Suite("domains", expect_at_least=30)

    # --- the catalogue ----------------------------------------------------
    s.check("restaurant is the default", domains.DEFAULT_CATEGORY == "restaurant")
    s.eq("an unknown key falls back to dinner rather than raising",
         domains.get("pickleballl").key, "restaurant")
    s.eq("None falls back too", domains.get(None).key, "restaurant")
    s.check("every category has at least one OSM selector",
            all(c.osm_selectors for c in domains.CATEGORIES.values()))
    s.check("every category names what it is booking",
            all(c.label and c.plural and c.verb for c in domains.CATEGORIES.values()))
    s.check("every category lists what the caller must settle on the phone",
            all(c.booking_fields for c in domains.CATEGORIES.values()))
    s.check("party size is a booking field everywhere it makes sense",
            all("party_size" in c.booking_fields
                for c in domains.CATEGORIES.values()))

    # Coverage honesty. Only restaurants were ever measured, and a category
    # claiming otherwise without a probe run would be the exact overclaim this
    # flag exists to prevent.
    s.eq("restaurants are the only measured category",
         [c.key for c in domains.verified_only()], ["restaurant"])

    # books_by is a claim about the real world. Courts in Hong Kong are mostly
    # LCSD facilities on SmartPLAY, and a voice agent phoning a government
    # pitch would be theatre.
    s.eq("courts are marked as platform-booked, not phone-booked",
         domains.get("court_sport").books_by, "platform")
    s.eq("restaurants are phone-booked", domains.get("restaurant").books_by, "phone")
    s.check("every books_by is one of the three known values",
            all(c.books_by in {"phone", "platform", "mixed"}
                for c in domains.CATEGORIES.values()))
    s.check("an unverified category explains itself in notes",
            all(c.notes for c in domains.CATEGORIES.values() if not c.coverage_verified))

    # --- query building ---------------------------------------------------
    bbox = (22.27, 114.13, 22.30, 114.22)
    for key, must_contain in [
        ("restaurant", "restaurant|fast_food"),
        ("court_sport", '"leisure"="pitch"'),
        ("karaoke", "karaoke"),
        ("party_room", "events_venue"),
    ]:
        q = domains.build_selector_query(domains.get(key), bbox)
        s.contains(f"{key} query selects the right tags", q, must_contain)
        s.check(f"{key} query carries a timeout", "timeout:" in q)
        s.contains(f"{key} query is bounded by the bbox", q, "22.27")

    # A pitch is a way, not a node. Querying only nodes was what made the first
    # court query come back empty.
    court_q = domains.build_selector_query(domains.get("court_sport"), bbox)
    s.check("court query asks for ways as well as nodes",
            "way[" in court_q and "node[" in court_q)

    s.ne("a court query does not ask for restaurants", 
         "restaurant" in court_q, True)

    # --- places.py, once it knows the category ----------------------------
    court = domains.get("court_sport")
    element = {
        "type": "way", "id": 42, "center": {"lat": 22.2783, "lon": 114.1747},
        "tags": {"name": "Southorn Playground", "leisure": "pitch",
                 "sport": "basketball;pickleball", "phone": "+852 2879 5622"},
    }
    row = places._row_from_element(element, court)
    s.eq("the descriptor comes from the category's own tag",
         row["descriptor"], "basketball, pickleball")
    s.eq("and it is labelled as what it is", row["descriptor_label"], "sport")
    s.eq("the row remembers its category", row["category"], "court_sport")
    s.eq("cuisine is left empty for a non-restaurant", row["cuisine"], "")
    s.eq("a real district still resolves", row["area"], "Wan Chai")
    s.eq("the phone still normalises", row["phone"], "+85228795622")

    # A missing descriptor must stay missing. Guessing "tennis" because most
    # pitches are tennis would put a lie in the agent's mouth on the phone.
    bare = places._row_from_element(
        {"type": "way", "id": 43, "center": {"lat": 22.2783, "lon": 114.1747},
         "tags": {"name": "Unnamed Pitch", "leisure": "pitch"}}, court)
    s.eq("an untagged court gets an empty descriptor, not a guess",
         bare["descriptor"], "")
    s.check("but it is still returned, because it is still a court",
            bare is not None and bare["name"] == "Unnamed Pitch")

    # Restaurants keep their old shape exactly, because everything else in the
    # repo still speaks `cuisine`.
    food = places._row_from_element(
        {"type": "node", "id": 44, "lat": 22.2783, "lon": 114.1747,
         "tags": {"name": "Samsen", "amenity": "restaurant", "cuisine": "thai"}})
    s.eq("restaurants still populate cuisine", food["cuisine"], "Thai")
    s.eq("and descriptor mirrors it", food["descriptor"], "Thai")

    # --- caches do not collide -------------------------------------------
    s.eq("the restaurant cache keeps its original filename",
         places.cache_path("restaurant"), places.CACHE_PATH)
    s.ne("another category gets its own file",
         places.cache_path("court_sport"), places.CACHE_PATH)
    s.contains("and the filename names the category",
               str(places.cache_path("party_room")), "party_room")

    # --- the seed list must not leak across categories --------------------
    # This is the one that matters. Overpass down + no cache used to fall back
    # to a hand-checked list of RESTAURANTS. Serving those to a group asking
    # about pickleball would produce a poll that looks perfectly fine and is
    # entirely wrong.
    court_cache = places.cache_path("court_sport")
    if court_cache.exists():
        court_cache.unlink()
    with FakeNet([url_error(), url_error(), url_error(),
                  url_error(), url_error(), url_error()], strict=False):
        courts = places.search_places(category="court_sport", limit=5)
    s.eq("no network and no court cache returns nothing at all", len(courts), 0)

    with FakeNet([url_error(), url_error(), url_error(),
                  url_error(), url_error(), url_error()], strict=False):
        food_rows = places.search_places(limit=5)
    s.check("whereas restaurants still fall back to the seed list",
            len(food_rows) > 0)
    s.check("and no seeded restaurant leaked into the court result",
            not any(r.get("name") in {f["name"] for f in food_rows} for r in courts))

    return s
