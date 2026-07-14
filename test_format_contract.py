"""The emote format contract, as a repeatable check.

This is the regression that actually matters: Kindroid renders anything outside
an emote as if the recipient said it. So a packet where another kin's dialogue
leaked out of its `_(* ... *)_` wrapper doesn't look untidy — it makes Eli think
he said Bobby's line. And a packet where Tim's words got "tidied up" puts words
in his mouth.

`coordinator._verify_packet` is the gate that catches both before a send. These
tests pin its behaviour so a future prompt tweak can't quietly disable it.

No API calls, no network — pure and fast:

    python test_format_contract.py
"""
import sys

from coordinator import _verify_packet
from kindroid_relay import _FORMAT_DIRECTIVE_EMOTE, parse_reply

LIMIT = 4000
TIM = "wait, hold on. did she just play him this entire time?"

D = _FORMAT_DIRECTIVE_EMOTE

CASES: list[tuple[str, str, str, bool, str]] = [
    # (name, body, what_tim_typed, should_pass, why it matters)
    (
        "canonical packet",
        f"{D}\n\n_(* The android doesn't blink. *)_\n\n{TIM}",
        TIM,
        True,
        "directive + emote + Tim's raw line is the shape we want",
    ),
    (
        "nested format directive alone",
        f"{D}\n\n{TIM}",
        TIM,
        True,
        "the directive quotes the markup it describes — it must not trip its own rule",
    ),
    (
        "another kin's speech, correctly wrapped",
        f'{D}\n\n_(* Bobby taps his nose and says, "She played him." *)_\n\n{TIM}',
        TIM,
        True,
        "third person + quoted speech, inside the emote",
    ),
    (
        "multiple emotes, one per paragraph",
        f"{D}\n\n_(* The room is dark. *)_\n\n_(* Bobby leans in. *)_\n\n{TIM}",
        TIM,
        True,
        "Kindroid's renderer needs one block per paragraph",
    ),
    (
        "reaction turn — Tim typed nothing",
        f"{D}\n\n_(* Tim reacts with a stunned exhale. *)_",
        "",
        True,
        "an emoji reaction has no raw text at all",
    ),
    # ---- the failures that matter ----
    (
        "Tim's words paraphrased",
        f"{D}\n\n_(* The android doesn't blink. *)_\n\nDid she play him the whole time?",
        TIM,
        False,
        "PUTS WORDS IN TIM'S MOUTH — the worst failure of the lot",
    ),
    (
        "Tim's words dropped entirely",
        f"{D}\n\n_(* The android doesn't blink. *)_",
        TIM,
        False,
        "his comment vanished from his own message",
    ),
    (
        "bare-star emote dialect",
        f"{D}\n\n*Bobby leans forward*\n\n{TIM}",
        TIM,
        False,
        "`*text*` leaks raw markup into the kin's face",
    ),
    (
        "paren-star emote dialect",
        f"{D}\n\n(*Bobby leans forward*)\n\n{TIM}",
        TIM,
        False,
        "`(*text*)` — same drift, different dialect",
    ),
    (
        "underscore-star dialect",
        f"{D}\n\n_*Bobby leans forward*_\n\n{TIM}",
        TIM,
        False,
        "`_*text*_` — same again",
    ),
    (
        "another kin's dialogue left unwrapped",
        f"{D}\n\n_(* Bobby taps his nose. *)_\n\nIdjit. She played him.\n\n{TIM}",
        TIM,
        False,
        "KINDROID WOULD THINK THE RECIPIENT SAID IT — the bug this whole thing exists to stop",
    ),
    (
        "over the character limit",
        f"{D}\n\n_(* {'x' * (LIMIT + 50)} *)_\n\n{TIM}",
        TIM,
        False,
        "the send would hard-fail at the API",
    ),
    (
        "empty body",
        "",
        TIM,
        False,
        "nothing to send",
    ),
]


def main() -> int:
    failures = 0
    print(f"emote format contract — {len(CASES)} cases\n")
    for name, body, typed, should_pass, why in CASES:
        problem = _verify_packet(body, dialogue=typed, limit=LIMIT)
        passed = problem is None
        ok = passed == should_pass
        if not ok:
            failures += 1
        mark = "ok  " if ok else "FAIL"
        verdict = "accepted" if passed else f"rejected: {problem}"
        print(f"  [{mark}] {name}")
        print(f"         expected {'accept' if should_pass else 'reject'} — {verdict}")
        if not ok:
            print(f"         ^^ {why}")

    # The regex parser must survive the dialects too — it's the fallback when
    # Claude is unreachable, so it can't be the weak link.
    print("\nregex fallback (parse_reply) still handles a messy reply:")
    _, _, segs = parse_reply('*leans in* Idjit. _(* taps nose *)_ Watch *Ex Machina*.')
    kinds = [s["type"] for s in segs]
    print(f"  segments: {kinds}")
    if "emote" not in kinds or "spoken" not in kinds:
        print("  FAIL — fallback parser produced no usable split")
        failures += 1
    else:
        print("  ok")

    print()
    if failures:
        print(f"{failures} FAILURE(S)")
        return 1
    print(f"all {len(CASES)} cases pass — the contract holds")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass
    raise SystemExit(main())
