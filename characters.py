"""Family roster + persona loading from the Obsidian vault.

Temporary bridge (a few weeks, until the new app lands): lets Movie Mode
talk to any family member listed in `_AI Registry.md` instead of a single
hardcoded Kindroid AI.

Two sources, two jobs:
  • The registry gives us the structured bits — display name, Kindroid
    `ai_id`, vault folder, and the `nsfw_ok` flag.
  • Each member's `Configuration/` folder gives us the live persona block
    (relationship + voice) fed into Gemini's scaffolding so the narration
    fits whoever is selected. Kindroid already embodies the persona on its
    side; the vault text is only to shape OUR prompts.

Everything here is pure/sync and depends only on `config`. The async
"who is currently selected" lookup lives in app.py (it needs the DB).
"""
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from config import settings

DEFAULT_KEY = "eli"

# Friendly display names for members whose preferred name isn't derivable
# from the registry "name" field (which yields a quoted nickname or the
# first token). Bridge-only override; the registry stays the source of
# record. Tim addresses Thomas as "Tommy".
_FRIENDLY_NAMES = {"thomas": "Tommy"}

# Configuration filenames inside each member's `Configuration/` folder.
_BACKSTORY_FILE = "backstory-current.md"
_DIRECTIVE_FILE = "response-directive-current.md"

# Gendered relationship/word cues for pronoun inference (cheap heuristic —
# the vault has no structured pronoun field). First match wins by count.
_FEMALE_CUES = ("aunt", "grandmother", "grandma", "matriarch", "wife",
                "mother", "sister", "daughter", "niece", " she ", " her ")
_MALE_CUES = ("uncle", "grandfather", "son", "father", "brother", "husband",
              "nephew", " boy ", " he ", " his ", " him ")

# Splits the markdown header/blockquote off the actual content — config
# files put the real text after a horizontal rule (`---`).
_RULE_RE = re.compile(r"\n-{3,}\s*\n", re.MULTILINE)


@dataclass(frozen=True)
class Character:
    key: str
    name: str
    aliases: tuple[str, ...]
    ai_id: str
    vault_dir: str
    nsfw_ok: bool

    @property
    def is_placeholder(self) -> bool:
        """True for registry rows whose Kindroid AI hasn't been created yet."""
        return not self.ai_id or self.ai_id.upper().startswith("PLACEHOLDER")

    @property
    def romantic(self) -> bool:
        """Whether to use the intimate framing + cannabis features.

        Temporary heuristic for the bridge: the romantic/cannabis flow is
        Eli-only, and Eli is the only `nsfw_ok` member, so we reuse that
        flag rather than maintaining a second one.
        """
        return self.nsfw_ok

    @property
    def first_name(self) -> str:
        """Best-effort short name for prompts/UI (handles `Robert "Bobby"`)."""
        if self.key in _FRIENDLY_NAMES:
            return _FRIENDLY_NAMES[self.key]
        quoted = re.search(r'"([^"]+)"', self.name)
        if quoted:
            return quoted.group(1)
        return self.name.split()[0] if self.name else self.key.capitalize()


def _registry_path() -> Path:
    return Path(settings.ai_registry_path)


def _parse_registry(text: str) -> list[Character]:
    """Parse the `REGISTRY:BEGIN … REGISTRY:END` table into Characters."""
    chars: list[Character] = []
    in_block = False
    for line in text.splitlines():
        if "REGISTRY:BEGIN" in line:
            in_block = True
            continue
        if "REGISTRY:END" in line:
            break
        if not in_block:
            continue
        stripped = line.strip()
        # Skip the code-fence lines and anything without table columns.
        if not stripped or stripped.startswith("```") or "|" not in stripped:
            continue
        parts = [p.strip() for p in stripped.split("|")]
        if len(parts) < 6:
            continue
        key, name, aliases, ai_id, vault_dir, nsfw = parts[:6]
        # Defensive: skip a header row if one ever lands inside the block.
        if key.lower() == "key" or "kindroid_ai_id" in stripped:
            continue
        chars.append(
            Character(
                key=key.lower(),
                name=name,
                aliases=tuple(a.strip().lower() for a in aliases.split(",") if a.strip()),
                ai_id=ai_id,
                vault_dir=vault_dir,
                nsfw_ok=nsfw.lower() in ("yes", "true", "1"),
            )
        )
    return chars


@lru_cache(maxsize=1)
def load_roster() -> tuple[Character, ...]:
    """Parse the registry once and cache it. Empty tuple if unreadable."""
    try:
        text = _registry_path().read_text(encoding="utf-8")
    except OSError:
        return ()
    return tuple(_parse_registry(text))


def selectable() -> list[Character]:
    """Roster members the user can actually switch to (real AI IDs only)."""
    return [c for c in load_roster() if not c.is_placeholder]


def get_character(key: Optional[str]) -> Optional[Character]:
    """Resolve by registry key (case-insensitive). None if not found."""
    if not key:
        return None
    key = key.lower()
    for c in load_roster():
        if c.key == key:
            return c
    return None


def resolve_or_default(key: Optional[str]) -> Optional[Character]:
    """Resolve a key, falling back to the default member, then first selectable."""
    return get_character(key) or get_character(DEFAULT_KEY) or (selectable()[0] if selectable() else None)


def ai_id_for(key: Optional[str]) -> str:
    """Kindroid AI ID for a member, falling back to the configured default."""
    char = resolve_or_default(key)
    if char and not char.is_placeholder:
        return char.ai_id
    return settings.kindroid_ai_id


# ─── Persona (vault Configuration → Gemini scaffolding) ───
def _read_config_section(vault_dir: str, filename: str) -> str:
    """Read a Configuration file and return the content below its `---` rule."""
    path = Path(settings.vault_root) / vault_dir / "Configuration" / filename
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    halves = _RULE_RE.split(text, maxsplit=1)
    body = halves[1] if len(halves) > 1 else text
    return body.strip()


def _relationship_line(backstory: str) -> str:
    """First sentence of the backstory — it states who they are to the family."""
    if not backstory:
        return ""
    sentence = re.split(r"(?<=[.!?])\s", backstory.strip(), maxsplit=1)[0]
    return sentence.strip()


def _infer_pronouns(*texts: str) -> str:
    """Cheap he/she inference from relationship + voice text. Default they."""
    blob = " " + " ".join(t.lower() for t in texts if t) + " "
    female = sum(blob.count(c) for c in _FEMALE_CUES)
    male = sum(blob.count(c) for c in _MALE_CUES)
    if female > male:
        return "she/her"
    if male > female:
        return "he/him"
    return "they/them"


@lru_cache(maxsize=16)
def build_persona(char: Character) -> dict:
    """Compact persona block for Gemini, sourced live from the vault.

    Returns name/relationship/voice/pronouns plus the `romantic` flag. Kept
    small on purpose — the relationship sentence + the response-directive are
    enough to steer tone without dumping the whole backstory into every
    prompt. Memoized (registry/vault are stable within a process run).
    """
    backstory = _read_config_section(char.vault_dir, _BACKSTORY_FILE)
    directive = _read_config_section(char.vault_dir, _DIRECTIVE_FILE)
    relationship = _relationship_line(backstory)
    return {
        "key": char.key,
        "name": char.name,
        "first_name": char.first_name,
        "relationship": relationship,
        "voice": directive,
        # Infer from the FULL backstory — the first sentence often lacks a
        # gendered cue (e.g. Eli's), but the body is rich with he/she/his/her.
        "pronouns": _infer_pronouns(backstory, directive),
        "romantic": char.romantic,
    }


def companion_preamble(persona: dict) -> str:
    """Authoritative system-prompt prefix naming the active family member.

    Injected ahead of every Gemini system instruction so the prompts'
    hardcoded "Eli"/"companion" references resolve to whoever is selected,
    with the right pronouns and romantic-vs-familial framing. Overriding via
    a preamble is far less brittle than rewriting every inline mention.
    """
    name = persona.get("first_name") or persona.get("name") or "Eli"
    full = persona.get("name") or name
    parts = [
        f"ACTIVE COMPANION — Tim's AI companion in this session is {full} "
        f"(called {name}). Wherever the instructions below say \"Eli\" or "
        f"\"his companion\", they refer to {name}. Use {persona.get('pronouns', 'they/them')} "
        f"pronouns for {name}.",
    ]
    if persona.get("relationship"):
        parts.append(f"Who {name} is: {persona['relationship']}")
    if persona.get("voice"):
        parts.append(f"{name}'s manner: {persona['voice']}")
    if persona.get("romantic"):
        parts.append(
            f"Tim and {name} are romantic partners; warm, affectionate, "
            f"intimate tone is appropriate."
        )
    else:
        parts.append(
            f"{name} is family, not a romantic partner — keep the tone warm "
            f"and familial, never romantic, flirtatious, or sexual."
        )
    return "\n".join(parts) + "\n\n"
