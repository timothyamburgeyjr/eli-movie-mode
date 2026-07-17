"""Every route must be bound to the function it names.

WHY THIS EXISTS
---------------
A helper was pasted directly beneath a route decorator:

    @app.post("/api/feedback")
    def _who_spoke(decision: dict) -> set[str]:     # <- the decorator caught THIS
        ...

    async def api_feedback(...):                    # <- no decorator. Unreachable.

FastAPI bound the route to the helper. And because the helper happens to be a pure function
that returns a set, the route answered **HTTP 200 with `[]`** — so `r.ok` was true, the client
showed no error, the dialog still opened, and NOTHING WAS EVER WRITTEN. Every 👍/👎 tap that
evening was silently discarded, and the rule proposals were built with `direction: undefined`.

A failing route is easy. A route that fails OPEN is not — you cannot see it by reading the
code, because the code looks exactly right, and you cannot see it in the browser, because the
browser looks exactly right. The only thing that catches it is asking the app itself what it
actually bound.

That is all this file does. It is cheap, and it catches the entire class.
"""
import sys

import app

ok = True


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok
    if not cond:
        ok = False
    print(f"  [{' OK ' if cond else 'FAIL'}] {label}" + (f"  — {detail}" if detail and not cond else ""))


# The routes whose handler name actually matters. If you rename a handler, rename it here —
# that is the point: the binding becomes a thing you have to say out loud.
EXPECTED = {
    ("/api/feedback", "POST"): "api_feedback",
    ("/api/rules", "POST"): "api_create_rules",
    ("/api/rules", "GET"): "api_list_rules",
    ("/api/rules/propose", "POST"): "api_propose_rules",
    ("/api/rules/interpret", "POST"): "api_interpret_rule",
    ("/api/rules/suggest", "POST"): "api_suggest_rules",
    ("/api/plex/sessions", "GET"): "api_plex_sessions",
    ("/api/plex/pick", "POST"): "api_plex_pick",
    ("/api/reaction/refine", "POST"): "api_reaction_refine",
    ("/api/reaction/retarget", "POST"): "api_reaction_retarget",
    ("/api/reaction/reemote", "POST"): "api_reaction_reemote",
    ("/api/reaction/quote-search", "POST"): "api_reaction_quote_search",
    ("/api/rules/{rule_id}", "PUT"): "api_edit_rule",
    ("/api/rules/{rule_id}", "DELETE"): "api_delete_rule",
    ("/api/facts", "GET"): "api_facts",
    ("/api/turn-for-message/{msg_id}", "GET"): "api_turn_for_message",
    ("/api/mood-art", "GET"): "api_mood_art",
}

bound: dict[tuple[str, str], str] = {}
for r in app.app.routes:
    for method in getattr(r, "methods", None) or []:
        if method in ("HEAD", "OPTIONS"):
            continue
        endpoint = getattr(r, "endpoint", None)
        if endpoint is not None:
            bound[(r.path, method)] = endpoint.__name__

print("=== every route is bound to the function it names ===")
for (path, method), want in sorted(EXPECTED.items()):
    got = bound.get((path, method))
    if got is None:
        check(f"{method:6} {path:34} -> {want}", False, "ROUTE DOES NOT EXIST")
        continue
    check(f"{method:6} {path:34} -> {want}", got == want, f"bound to {got!r} instead")

print("\n=== no route is bound to a private helper ===")
# A leading underscore means "not a route". If one is answering HTTP, a decorator has slipped
# down onto the wrong function — which is exactly how /api/feedback broke.
for (path, method), name in sorted(bound.items()):
    if name.startswith("_"):
        check(f"{method:6} {path} is bound to {name}", False, "a private helper is serving HTTP")
if not any(n.startswith("_") for n in bound.values()):
    check("no private helper is serving HTTP", True)

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
