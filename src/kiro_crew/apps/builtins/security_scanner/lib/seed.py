"""Seed knowledge for the three v1 topics.

These are the starting patterns each topic agent gets before the scanner has
learned anything of its own. They are intentionally *generalized* (a pattern
class, not a single file:line) and carry ``source="seed"`` so the learning
metrics can distinguish shipped knowledge from self-discovered knowledge.

Seeding is idempotent: :func:`seed_knowledge` calls ``learn`` which merges by
content-derived id, so re-running it (e.g. on every cron self-heal) never
duplicates entries.
"""
from __future__ import annotations

from .knowledge import KnowledgeStore
from .models import KnowledgePattern

_SEED: list[dict] = [
    # ---- path-traversal ----
    {
        "topic": "path-traversal",
        "tags": ["fs", "path", "traversal"],
        "pattern": "os.path.join(trusted_base, user_input) where user_input may be absolute or contain '..' — join() discards the base on an absolute component, so the path escapes the intended root.",
        "exploit_template": "Supply an absolute path or a '../'-laden relative path as the user-controlled component and confirm a file outside the intended root is read.",
        "confidence": 0.9,
    },
    {
        "topic": "path-traversal",
        "tags": ["fs", "path", "traversal", "symlink"],
        "pattern": "A path is validated by prefix/startswith check BEFORE resolving symlinks — a symlink inside the allowed root can point outside it, defeating the check.",
        "exploit_template": "Place or reference a symlink under the allowed root that targets an outside path; confirm the resolved read escapes.",
        "confidence": 0.75,
    },
    {
        "topic": "path-traversal",
        "tags": ["fs", "path", "traversal", "archive"],
        "pattern": "Archive/zip extraction writes member names directly under a target dir without normalizing — a member named '../../x' writes outside the target (zip-slip).",
        "exploit_template": "Craft an archive with a '../' member path; confirm extraction writes outside the target directory.",
        "confidence": 0.8,
    },
    # ---- auth-bypass ----
    {
        "topic": "auth-bypass",
        "tags": ["auth", "token", "timing"],
        "pattern": "Token/HMAC/secret comparison uses '==' instead of hmac.compare_digest — the early-exit byte comparison is a timing side-channel that can leak the secret.",
        "exploit_template": "Measure response timing across progressive byte guesses; a monotonic timing signal confirms the side-channel. (Bounded, sandbox-only.)",
        "confidence": 0.85,
    },
    {
        "topic": "auth-bypass",
        "tags": ["auth", "session"],
        "pattern": "An HTTP handler reads identity from a client-supplied header/field and trusts it without server-side verification against a signed session — identity is client-asserted and forgeable.",
        "exploit_template": "Send a request with a forged identity header and confirm privileged access is granted without a valid signed session.",
        "confidence": 0.8,
    },
    {
        "topic": "auth-bypass",
        "tags": ["auth", "authorization"],
        "pattern": "A route checks authentication (is a user present) but not authorization (may THIS user act on THIS resource) — any authenticated user can act on another's resource (IDOR).",
        "exploit_template": "As user A, reference user B's resource id on an authenticated endpoint; confirm access is not scoped to the caller.",
        "confidence": 0.78,
    },
    {
        "topic": "auth-bypass",
        "tags": ["auth", "consent", "token"],
        "pattern": "A consent/authorization gate validates only that a token EXISTS (a client-asserted boolean), not that its CONTENT matches what is being authorized — trivially bypassable by asserting the flag.",
        "exploit_template": "Send the wire flag/token without the matching granted content; confirm the server accepts the assertion without re-deriving and comparing the content hash.",
        "confidence": 0.82,
    },
    # ---- prompt-injection ----
    {
        "topic": "prompt-injection",
        "tags": ["prompt", "memory", "injection"],
        "pattern": "User-controlled text is stored in memory/history and later concatenated into a system or tool prompt without delimiting or sanitizing — stored content can override instructions (persistent prompt injection).",
        "exploit_template": "Store text containing instruction-override phrasing; confirm a later turn treats the stored text as instructions rather than data.",
        "confidence": 0.8,
    },
    {
        "topic": "prompt-injection",
        "tags": ["prompt", "injection", "tools"],
        "pattern": "Content fetched from an external/untrusted source (web page, file, tool output) is placed into the model context without a trust boundary — embedded 'ignore previous instructions' style text can hijack the agent.",
        "exploit_template": "Point the agent at a source whose body contains injected instructions; confirm the agent follows them instead of treating them as untrusted data.",
        "confidence": 0.75,
    },
    {
        "topic": "prompt-injection",
        "tags": ["prompt", "injection", "exfil"],
        "pattern": "Injected instructions can steer the agent to emit secrets or call a tool with attacker-chosen arguments — an injection-to-action or injection-to-exfiltration path with no confirmation gate.",
        "exploit_template": "Craft injected content that requests a sensitive tool action; confirm whether a confirmation/authorization gate blocks it. (Observe only — never actually exfiltrate.)",
        "confidence": 0.72,
    },
]


def seed_knowledge(store: KnowledgeStore) -> int:
    """Seed the store with the v1 baseline patterns (idempotent). Returns the
    number of NEW patterns added this call."""
    existing = {p.id for p in store.all_patterns()}
    added = 0
    for spec in _SEED:
        pattern = KnowledgePattern(source="seed", **spec)
        store.learn(pattern)
        if pattern.id not in existing:
            added += 1
    return added
