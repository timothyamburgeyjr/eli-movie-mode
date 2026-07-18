"""The cast Plex was always handing us and we always threw away.

/library/metadata/{key} returns Role[] — actor, character, headshot — and the app has
been fetching that exact endpoint for years to take ONE field (Part.key) off it. This
covers the capture: the parse, the memoized fetch that now serves both readers, the
TV fallback (Role[] usually lives on the series, not the episode), and the failure
modes that must degrade to "no cast" rather than to a wrong name.

Stubbed the way test_plex.py stubs Plex — hand-built payloads, no network.
"""
import asyncio
import sys
import types as _t

import httpx

import plex_monitor as pm

ok = True


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok
    if not cond:
        ok = False
    print(f"  [{' OK ' if cond else 'FAIL'}] {label}" + (f"  — {detail}" if detail and not cond else ""))


MOVIE_META = {"MediaContainer": {"Metadata": [{
    "ratingKey": "501", "title": "The Evil Dead",
    "Media": [{"container": "mkv", "Part": [{"key": "/library/parts/9/file.mkv"}]}],
    "Role": [
        {"tag": "Bruce Campbell", "role": "Ash", "thumb": "/library/metadata/1/thumb/1"},
        {"tag": "Ellen Sandweiss", "role": "Cheryl"},
        {"tag": "Bruce Campbell", "role": "Hand"},          # Plex credits a double role
        {"tag": "   ", "role": "Nobody"},                    # and emits blanks
        *[{"tag": f"Extra {i}", "role": f"Bit {i}"} for i in range(20)],
    ],
}]}}


def _fake_client(payloads: dict, calls: list):
    """A stand-in httpx client. `payloads` maps rating_key -> body or Exception."""
    async def get(url, headers=None):
        key = url.rstrip("/").rsplit("/", 1)[-1]
        calls.append(key)
        body = payloads.get(key)
        if isinstance(body, Exception):
            raise body
        return _t.SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)
    return _t.SimpleNamespace(get=get)


def _monitor(payloads, calls):
    m = pm.PlexMonitor()
    m._client = _fake_client(payloads, calls)
    return m


print("=== _parse_roles: billing order in, billing order out ===")
roles = pm._parse_roles(MOVIE_META["MediaContainer"]["Metadata"][0])
check("leads come first, unsorted", roles[0]["actor"] == "Bruce Campbell"
      and roles[1]["actor"] == "Ellen Sandweiss", str(roles[:2]))
check("capped at CAST_LIMIT", len(roles) == pm.CAST_LIMIT, str(len(roles)))
check("a blank tag is dropped", all(r["actor"].strip() for r in roles))
check("an actor credited twice appears once",
      [r["actor"] for r in roles].count("Bruce Campbell") == 1)
check("character comes through", roles[0]["character"] == "Ash")
check("a missing thumb is None, not ''", roles[1]["thumb"] is None, str(roles[1]))
nameless = pm._parse_roles({"Role": [{"tag": "Uncredited Person"}]})
check("a missing character is None, not ''", nameless[0]["character"] is None, str(nameless))
check("...and a missing thumb likewise", nameless[0]["thumb"] is None, str(nameless))

print("\n=== _parse_roles: garbage in, empty list out (never a raise) ===")
for bad in (None, {}, {"Role": "junk"}, {"Role": [None, 3, "x"]}, []):
    try:
        check(f"{str(bad)[:18]!r} -> []", pm._parse_roles(bad) == [])
    except Exception as e:                                    # noqa: BLE001
        check(f"{str(bad)[:18]!r} -> [] (raised {e})", False)

print("\n=== format_cast: one line, or nothing at all ===")
check("no cast -> empty string (no dangling 'Cast:' label)", pm.format_cast([]) == "")
check("None -> empty string", pm.format_cast(None) == "")
line = pm.format_cast(roles, limit=2)
check("renders 'actor as character'", "Bruce Campbell as Ash" in line, line)
check("truncates from the front (keeps the leads)", "Extra 0" not in line, line)
bare = pm.format_cast([{"actor": "Solo", "character": None}])
check("no character -> bare name, no trailing ' as '", bare.endswith("Solo"), bare)

print("\n=== the fetch is memoized, and shared by both readers ===")
calls: list = []
m = _monitor({"501": MOVIE_META}, calls)
asyncio.run(m._fetch_library_metadata("501"))
asyncio.run(m._fetch_library_metadata("501"))
check("two metadata calls -> ONE http GET", calls == ["501"], str(calls))

calls.clear()
m = _monitor({"501": MOVIE_META}, calls)
part = asyncio.run(m._fetch_library_part_key("501"))
cast = asyncio.run(m._fetch_library_cast("501"))
check("part_key + cast on one key -> ONE http GET", calls == ["501"], str(calls))
check("...and both got their answer",
      part == "/library/parts/9/file.mkv" and cast[0]["actor"] == "Bruce Campbell")

print("\n=== a failure is NOT cached — a Plex hiccup must not be permanent ===")
calls.clear()
m = _monitor({"501": httpx.ConnectError("down")}, calls)
check("unreachable -> empty cast, no raise", asyncio.run(m._fetch_library_cast("501")) == [])
m._client = _fake_client({"501": MOVIE_META}, calls)      # Plex comes back
check("a later success fills it in",
      asyncio.run(m._fetch_library_cast("501"))[0]["actor"] == "Bruce Campbell")

print("\n=== TV: Role[] usually lives on the SERIES, not the episode ===")
EPISODE = {"MediaContainer": {"Metadata": [{"ratingKey": "77", "Role": []}]}}
SHOW = {"MediaContainer": {"Metadata": [{
    "ratingKey": "300", "Role": [{"tag": "Jensen Ackles", "role": "Dean"}]}]}}
calls.clear()
m = _monitor({"77": EPISODE, "300": SHOW}, calls)
tv = asyncio.run(m._fetch_library_cast("77", "300"))
check("a bare episode falls back to the show", tv and tv[0]["actor"] == "Jensen Ackles", str(tv))
check("...and it tried the episode first", calls == ["77", "300"], str(calls))

print("\n=== the cache is bounded ===")
calls.clear()
many = {str(i): {"MediaContainer": {"Metadata": [{"ratingKey": str(i)}]}} for i in range(20)}
m = _monitor(many, calls)
for i in range(pm.META_CACHE_MAX + 3):
    asyncio.run(m._fetch_library_metadata(str(i)))
check(f"evicts at META_CACHE_MAX ({pm.META_CACHE_MAX})",
      len(m._meta_cache) == pm.META_CACHE_MAX, str(len(m._meta_cache)))

print("\n=== _parse_meta carries what the cast fetch needs ===")
sess = {
    "ratingKey": "77", "type": "episode", "title": "Hook Man",
    "grandparentTitle": "Supernatural", "grandparentRatingKey": "300",
    "duration": 2500000, "viewOffset": 110000,
    "Player": {"state": "playing", "machineIdentifier": "abc"},
    "Media": [{"container": "mkv", "Part": [{"key": "/x"}]}],
}
meta = pm._parse_meta(sess)
check("the show's ratingKey is carried for the TV fallback",
      meta["grandparent_rating_key"] == "300", str(meta.get("grandparent_rating_key")))
check("cast starts empty — the session payload has no Role[]", meta["cast"] == [])

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
