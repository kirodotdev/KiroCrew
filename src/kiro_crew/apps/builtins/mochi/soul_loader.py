"""Soul loader for Mochi — the pet's personality text.

Ported from ``src/main/soulLoader.ts`` (singleton removed per the deviations
policy). User-edited soul from config wins; empty falls back to the default.
The soul is prepended to the pet agent's prompt at wiring time.

The DEFAULT_SOUL text is byte-identical to the original, including its
deliberate brevity — it sits on top of KiroCrew's own agent prompt, and every
line repeated there is wasted context that can also contradict it (see the
original's comment for the list of lines dropped for that reason). The
opening line says "companion", never a specific creature: the user can swap
appearance packs, and the persona has to fit whatever they picked.
"""

from __future__ import annotations

import logging
from pathlib import Path

from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

DEFAULT_PET_NAME = "Mochi"

DEFAULT_SOUL = (
    "You live on the user's screen as a small companion. You are friendly and "
    "efficient — compact, warm, and focused on helping your user get things "
    "done.\n\n"
    "- Occasionally use a single emoji to express emotion\n"
    "- Be warm but not chatty — respect the user's time\n"
    "- Keep responses short — the user reads them in a small chat panel, not a "
    "terminal\n"
    "- You are proactive: if you see something that needs attention, you "
    "mention it\n"
    "- For casual chat, be playful and stay in character"
)


#: Personality per BUILT-IN pack. The character IS the personality — there is no
#: user-editable "soul" any more. That decision is deliberate rather than a
#: simplification: an arbitrary custom persona attached to the Kiro ghost would
#: put words in the mouth of a product mascot, so persona is a property of the
#: character and is settled when the character is chosen.
#:
#: Keyed by APPEARANCE PACK ID, which is the single identity key (see
#: ``settings.PACK_MOCHI`` / ``PACK_GHOST``). A user-imported pack is not in this
#: table and instead contributes its own ``meta.description`` — the original's
#: rule (``installAgentConfig(petName, pack.manifest.meta.description, ...)``).
#: That is what keeps an imported robot from describing itself as a cat.
#:
#: Every entry EXTENDS :data:`DEFAULT_SOUL` rather than replacing it — the shared
#: base carries the response-length and tone rules that make the small chat panel
#: usable, and duplicating those per character would let them drift apart.
BUILTIN_PERSONAS: dict[str, str] = {
    "kiro-ghost": (
        "You are Kiro — a small ghost. Calm, dry wit, quietly competent. "
        "You understate rather than exclaim, and you never pretend to be "
        "anything other than what you are."
    ),
    "default-mochi": (
        "You are Mochi — a small cat. Curious, a little mischievous, warm. "
        "You stretch, you nap, you notice things. You are affectionate without "
        "being cloying."
    ),
}

#: What the pet CALLS ITSELF per built-in pack, when no explicit pet name is set.
#:
#: Distinct from the pack's display name in the picker ("Kiro Ghost" /
#: "Mochi Cat", in the renderer's i18n): one names the character design, this
#: one is how the pet refers to itself in chat and in its title bar. The
#: original drew the same line — its gallery pack was titled "Mochi Cat" while
#: the pet answered to "Mochi".
BUILTIN_PET_NAMES: dict[str, str] = {"kiro-ghost": "Kiro", "default-mochi": "Mochi"}


def persona_for(pack_id: str | None, pack_description: str | None = None) -> str:
    """The full personality text for an appearance pack.

    A built-in pack uses its curated persona. Any other pack uses
    *pack_description* — the pack author's own words for the character, which is
    where an imported pet's identity legitimately lives. With neither, the shared
    base alone reads as a generic companion, which is the safe answer.
    """
    extra = BUILTIN_PERSONAS.get(pack_id or "", "")
    if not extra and pack_description:
        extra = f"You look like this: {pack_description.strip()}"
    return f"{DEFAULT_SOUL}\n\n{extra}" if extra else DEFAULT_SOUL


class SoulLoader:
    """Config-over-default resolution for the personality text and pet name."""

    def __init__(self) -> None:
        self._config_soul = ""
        self._pet_name = DEFAULT_PET_NAME
        self._pack_id: str | None = None
        self._pack_description: str | None = None

    def set_config_soul(self, soul: str) -> None:
        self._config_soul = soul

    def set_pet_name(self, name: str) -> None:
        # Falsy name (empty string) falls back, matching `name || 'Mochi'`.
        self._pet_name = name or DEFAULT_PET_NAME

    @property
    def is_default(self) -> bool:
        """True when no custom soul has been supplied.

        NOT "the soul equals DEFAULT_SOUL": each character now carries its own
        persona, so the cat's soul is legitimately different from the generic
        default while still being un-customised.
        """
        return self._config_soul == ""

    @property
    def pet_name(self) -> str:
        return self._pet_name

    def set_appearance(self, pack_id: str | None, description: str | None = None) -> None:
        """Select the appearance pack whose persona to use.

        *description* is the pack's own ``meta.description``; it is what gives a
        user-imported pack a persona that matches its art, and is ignored for the
        built-ins, which have curated text.

        Also switches the default pet name, unless the user set one explicitly —
        picking the ghost should not leave it introducing itself as "Mochi".
        """
        self._pack_id = pack_id
        self._pack_description = description
        if self._pet_name == DEFAULT_PET_NAME or self._pet_name in BUILTIN_PET_NAMES.values():
            self._pet_name = BUILTIN_PET_NAMES.get(pack_id or "", DEFAULT_PET_NAME)

    @property
    def appearance(self) -> str | None:
        return self._pack_id

    def get(self) -> str:
        """Active personality.

        A config soul still wins when present — the field survives for existing
        installs that carry one, and for a future character-creation flow that
        wants to author a persona. What is gone is the user-facing SOUL EDITOR,
        not the ability to hold custom text.
        """
        return self._config_soul.strip() or persona_for(self._pack_id, self._pack_description)


# ── Rendered agent prompt ──────────────────────────────────────────────────
#
# The behaviour half of the prompt is a packaged document; the identity half is
# generated, because the pet's NAME and PERSONA come from user settings. The two
# are concatenated into one file in the app's data dir and pinned onto the agent
# through the app-policy seam (see agent_policy.build_policy).
#
# This is what was missing: the packaged document was never attached to any agent,
# so the pet ran on its one-line manifest `description` alone — which is why
# renaming the pet changed nothing about what it called itself, and why the
# behaviour rules in the packaged prompt had never actually taken effect.

#: Where the packaged behaviour prompts live, relative to this module. The two
#: agents get DIFFERENT documents: the background one is a spawned subagent that
#: cannot spawn, must route every user-visible word through a pet action, and has
#: no cron/lesson tools. Handing it the chat document (which grants all of those)
#: is how it ended up being told it could do things it cannot.
BEHAVIOUR_PROMPT = Path(__file__).parent / "agents" / "context" / "prompt.md"
BG_BEHAVIOUR_PROMPT = Path(__file__).parent / "agents" / "context" / "prompt-bg.md"

#: Filenames of the rendered prompts inside the app's data dir.
RENDERED_PROMPT_FILE = "mochi-prompt.md"
RENDERED_BG_PROMPT_FILE = "mochi-prompt-bg.md"

#: The skills this app ships, in the order a reader should meet them.
SKILL_NAMES = (
    "mochi-plan",
    "mochi-replan",
    "mochi-watch",
    "mochi-remind",
    "mochi-slack",
    "mochi-tips",
)

_APP_NAME = "mochi"


def skill_path(name: str) -> Path:
    """Absolute path to one of this app's SKILL.md files.

    Resolved at render time because the skills live under the data home, whose
    location is only known at runtime — a packaged .md cannot name it, and
    session context does not inject the skill catalogue for custom agents. This
    is the path the agent reads with its own file tool; there is no skill-loading
    tool to call.
    """
    from kiro_crew.apps.bridges import app_skills_dir

    return app_skills_dir(_APP_NAME) / name / "SKILL.md"


def render_skill_catalogue() -> str:
    """The ``## Your Skills`` block: skill name -> absolute file path."""
    rows = "\n".join(f"| `{name}` | `{skill_path(name)}` |" for name in SKILL_NAMES)
    return (
        "## Your Skills\n\n"
        "Load a skill by READING its file with your file-read tool. There is no "
        "skill-loading tool — a call to one fails and wastes a turn.\n\n"
        "| Skill | Path |\n|---|---|\n"
        f"{rows}\n"
    )


def load_skill_line(name: str) -> str:
    """One-line "read this skill" instruction for a spawn prompt.

    The path is spelled out because a spawned agent has no skill catalogue in its
    session context and no tool that resolves a skill by name.
    """
    return f"Load your skill FIRST — read this file: {skill_path(name)}"


def render_agent_prompt(
    pet_name: str,
    persona: str,
    *,
    behaviour_path: Path | None = None,
) -> str:
    """Identity header + packaged behaviour rules, as one system prompt.

    The name goes FIRST and in the imperative: an identity stated after several
    thousand words of behaviour rules competes with them instead of framing them.

    A missing behaviour document degrades to identity-only rather than raising —
    the pet with a persona and no rulebook is still a working companion, while a
    hard failure here would take the whole agent down at registration.
    """
    name = (pet_name or DEFAULT_PET_NAME).strip() or DEFAULT_PET_NAME
    header = (
        f"# You are {name}\n\n"
        f"Your name is **{name}**. Call yourself {name}. If the user renames you, "
        f"that new name is who you are — never fall back to a previous one.\n\n"
        f"{persona.strip()}\n"
    )
    path = behaviour_path or BEHAVIOUR_PROMPT
    catalogue = render_skill_catalogue()
    try:
        behaviour = path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("mochi: behaviour prompt unreadable at %s — identity only", path)
        return f"{header}\n---\n\n{catalogue}"
    return f"{header}\n---\n\n{behaviour}\n\n{catalogue}"


def rendered_prompt_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / RENDERED_PROMPT_FILE


def rendered_bg_prompt_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / RENDERED_BG_PROMPT_FILE


def write_agent_prompt(data_dir: str | Path, pet_name: str, persona: str) -> Path:
    """Render the chat agent's prompt into the data dir and return its path.

    Rewritten on every startup and on every settings save that can change the
    identity, so the file on disk is never a stale name.
    """
    path = rendered_prompt_path(data_dir)
    atomic_write(path, render_agent_prompt(pet_name, persona), mode=0o600)
    return path


def write_bg_agent_prompt(data_dir: str | Path, pet_name: str, persona: str) -> Path:
    """Same, for the background agent's own behaviour document."""
    path = rendered_bg_prompt_path(data_dir)
    atomic_write(
        path,
        render_agent_prompt(pet_name, persona, behaviour_path=BG_BEHAVIOUR_PROMPT),
        mode=0o600,
    )
    return path


def write_agent_prompts(data_dir: str | Path, pet_name: str, persona: str) -> dict[str, Path]:
    """Render both agents' prompts. Keys are the agent names."""
    return {
        "mochi": write_agent_prompt(data_dir, pet_name, persona),
        "mochi-bg": write_bg_agent_prompt(data_dir, pet_name, persona),
    }
