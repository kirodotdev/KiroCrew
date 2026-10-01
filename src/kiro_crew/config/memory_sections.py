"""Sections for what Kiro Crew keeps and derives across sessions.

Owns the DTOs, defaults and field coercion for ``memory``, ``knowledge``,
``skills``, ``session_summary`` and the named ``memory_stores`` records, plus the
raw-dict merge a store override applies over the top-level ``memory`` section.
``config.sections`` re-exports every name; this module never imports it, the
loader, schema or validation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kiro_crew.config.fields import _meta
from kiro_crew.effort import EFFORT_LEVELS

logger = logging.getLogger("kiro_crew.config.loader")


@dataclass
class MemoryConfig:
    embedding_provider: str = field(
        default="llama_cpp",
        metadata=_meta(
            "Embedding Provider",
            "Vector embedding backend (always-on). In-process via vendored llama-cpp-python. "
            "Legacy configs with 'ollama' or 'none' are auto-migrated to 'llama_cpp'.",
            enum=["llama_cpp"],
        ),
    )
    embedding_dim: int = field(
        default=1024,
        metadata=_meta(
            "Embedding Dimension",
            "Dimensionality of embedding vectors. Changing it changes the vector "
            "space, so every stored embedding must be regenerated -- applied by the "
            "Settings embedding-model action, not by a plain config write.",
            restart=True,
        ),
    )
    embedding_threads: int = field(
        default=4,
        metadata=_meta(
            "Embedding Threads",
            "CPU threads for an explicit memory query or user-started re-embedding. "
            "Defaults to 4, capped one core below the CPUs this process may run on -- "
            "never below one thread, so a single-core host still embeds -- to leave the "
            "event loop a core wherever there is one to spare; 4 means that default, "
            "so pinning threads where the process may use 4 or fewer CPUs takes "
            "another number. Any other "
            "setting is honoured up to that count, which a CPU-set restriction "
            "(--cpuset-cpus, taskset) narrows; a CFS quota (--cpus, cpu.max) sets "
            "no mask and is not seen, while a limit a scheduler turns into an "
            "exclusive cpuset is. All memory stores share one "
            "model and inference worker. "
            "V2 message context does not run an embedding search; V1 retains "
            "its session-start retrieval.",
        ),
    )
    embedding_bulk_threads: int = field(
        default=1,
        metadata=_meta(
            "Embedding Threads (bulk)",
            "CPU threads for BACKGROUND corpus embedding — the re-embed sweep that "
            "gives imported memories semantic reach, plus imports and consolidation — "
            "as opposed to a query you are waiting on. Defaults to 1: nothing waits on "
            "this work (those rows are keyword-searchable meanwhile), so it is tuned to "
            "use fewer resources rather than finish early. Both classes share one "
            "inference worker; waiting interactive queries take priority. 0 means "
            "inherit Embedding Threads. Explicit settings are honoured up to "
            "the CPUs this process may run on.",
        ),
    )
    embedding_bulk_duty: float = field(
        default=0.2,
        metadata=_meta(
            "Embedding Duty Cycle (bulk)",
            "Fraction of wall time a background embedding sweep targets for computing. "
            "At the default 0.2 the shared worker targets an idle interval four times "
            "as long as each bulk inference. Parallel stores share this pacing; "
            "interactive queries can interrupt the idle interval. "
            "The sweep resumes across restarts, so it need not finish in one session. "
            "A target rather than a ceiling: one unusually slow row is capped at a "
            "30-second pause and so runs hotter than the configured share. 1.0 runs "
            "flat out. Clamped to [0.05, 1.0]; a sweep a user explicitly starts from "
            "Settings is never paced.",
        ),
    )
    embed_model_url: str = field(
        default="",
        metadata=_meta(
            "Embedding Model URL",
            "Override HTTPS URL for the embedding model GGUF download (mirrored/airgapped "
            "deployments). Empty uses the public Kiro Crew CDN default; the "
            "KIROCREW_EMBED_MODEL_URL env var wins over both. The download is "
            "sha256-verified regardless of source.",
        ),
    )
    embed_model_path: str = field(
        default="",
        metadata=_meta(
            "Embedding Model Path",
            "Absolute path to a local GGUF embedding model to use INSTEAD of the bundled "
            "Qwen3-Embedding-0.6B. When set, the default model is never downloaded or "
            "installed, so a custom model survives a default-model version change. Set "
            "embedding_dim to the model's output width. Changing the model changes the "
            "vector space, so stored embeddings are regenerated automatically. The "
            "KIROCREW_EMBED_MODEL_PATH env var wins over this.",
            restart=True,
        ),
    )
    embed_model_id: str = field(
        default="",
        metadata=_meta(
            "Embedding Model ID",
            "Optional label for a custom model. The vector-space identity is "
            "'<label>:sha256:<digest>' of the model file's bytes, so different models "
            "of identical name and size are always told apart; this key cannot pin or "
            "override that identity. Applying a model from the dashboard writes the "
            "resulting id together with embed_model_stamp.",
            restart=True,
        ),
    )
    embed_model_stamp: list[int] = field(
        default_factory=list,
        metadata=_meta(
            "Embedding Model File Stamp",
            "Managed file identity for verified custom weights: device, inode, byte size, "
            "modification nanoseconds and change nanoseconds. An empty list means unverified.",
        ),
    )
    embed_rebuild_generation: str = field(
        default="",
        metadata=_meta(
            "Embedding Rebuild Generation",
            "Managed explicit-apply request identity. Stores acknowledge it only after "
            "invalidating their previous vectors; empty preserves ordinary upgrade behavior.",
        ),
    )
    embed_model_legacy_ids: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Embedding Model Legacy IDs",
            "Managed compatibility labels mapped to embed_model_id and embed_model_stamp. "
            "Preserves matching stored vectors across restarts; cleared when the model identity changes.",
        ),
    )
    semantic_confidence_threshold: float = field(
        default=0.8,
        metadata=_meta(
            "Semantic Confidence Threshold",
            "Minimum similarity score for semantic search results.",
        ),
    )
    episodic_dedup_threshold: float = field(
        default=0.88,
        metadata=_meta(
            "Episodic Dedup Threshold",
            "Similarity threshold for deduplicating episodic memories.",
        ),
    )
    episodic_max_results: int = field(
        default=8,
        metadata=_meta("Episodic Max Results", "Maximum episodic memory results per query."),
    )
    episodic_max_count: int = field(
        default=10_000,
        metadata=_meta(
            "Episodic Max Count",
            "V1 episodic storage cap. V2 retains memories without automatic capacity eviction.",
        ),
    )
    decay_rates: dict[str, float] = field(
        default_factory=dict,
        metadata=_meta(
            "Memory Decay Rates",
            "V1 only; V2 recall scores do not decay with age. "
            "Per-tag episodic recency decay rates, per day (retrieval score factor "
            "exp(-rate * days_old)). Keys are memory tags (case-insensitive); the "
            "reserved 'default' key replaces the built-in 0.03 for memories matching "
            "no configured tag. A memory carrying several configured tags uses the "
            "slowest (smallest) rate, so a broad tag can never age out a "
            "long-retention one. 0 means never ages out of retrieval ranking; 1 "
            "drops a memory out of retrieval within about a day. Ranking only: "
            "episodic_max_count cap eviction (lowest importance, then oldest) "
            "still applies regardless of decay rate. Values are clamped to 0..10; "
            "non-numeric values are ignored with a logged warning.",
        ),
    )
    semantic_keys: list[str] = field(
        default_factory=list,
        metadata=_meta("Semantic Keys", "Keys to index for semantic search."),
    )
    history_idle_hours: float = field(
        default=3.0,
        metadata=_meta(
            "History Idle Hours",
            "Hours of inactivity before history consolidation.",
        ),
    )
    history_max_days: int = field(
        default=365,
        metadata=_meta("History Max Days", "Maximum days of history to retain."),
    )
    backup_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Automatic Memory Backups",
            "Take a daily rotating copy of every active memory store: the default "
            "store, named V1 stores and member V2 stores. This does not delete active "
            "memories.",
        ),
    )
    backup_keep: int = field(
        default=7,
        metadata=_meta(
            "Memory Backups Kept",
            "How many backups to keep per store after an automatic or requested backup. "
            "Values below 1 are treated as 1 so retention cannot empty the directory.",
        ),
    )
    persistence_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Persistence Enabled",
            "Global switch for persistent memory. Off: no automatic memory "
            "writes (lessons, consolidation extraction, task-runner lessons) "
            "and no stored memory/lessons injected into new sessions; "
            "within-conversation context is unaffected. Explicit dashboard "
            "edits and deletions stay available. An installed app's own "
            "ingestion sweep is out of scope and still writes app-scoped rows.",
        ),
    )
    inject_memory: bool = field(
        default=True,
        metadata=_meta(
            "Inject Memory Context",
            "Inject the stored memory block (preferences, the memory activity "
            "index and recent-session snippets) into new-session context. "
            "On-demand memory_recall is unaffected.",
        ),
    )
    inject_lessons: bool = field(
        default=True,
        metadata=_meta(
            "Inject Lessons Context",
            "Inject the learned-corrections and user-profile blocks into " "new-session context.",
        ),
    )
    inject_lessons_per_turn: bool = field(
        default=False,
        metadata=_meta(
            "Lessons Per Message",
            "On each follow-up message, add up to three stored lessons that match it "
            "and have not been shown in the session yet. The session-start block "
            "holds only what fits its budget; this adds the rest as the topic "
            "reaches them. Requires inject_lessons.",
        ),
    )
    inject_activity: bool = field(
        default=True,
        metadata=_meta(
            "Inject Memory Activity",
            "Inject the recent activity block (active projects, daily history (14 full "
            "days, then decayed summaries and counts to day 180), task facts and "
            "relevant past episodes) into new-session context as a "
            "budgeted background block. Off: only preferences and the activity index "
            "ship at session start and older material is read through memory_recall. "
            "Requires inject_memory.",
        ),
    )
    migrated: bool = field(
        default=False,
        metadata=_meta("Migrated", "Whether memory has been migrated to vector store."),
    )


#: Default artifact kinds eligible for Knowledge Library auto-ingest. These are
#: the substantial-document kinds whose content the KB file reader can extract
#: (routed through the same reader as folders/uploads): markdown/text/json read
#: as text, and html goes through HTML prose extraction. ``widget`` is excluded
#: -- widgets/dashboards are UI, not documents (and a remote widget round-trips
#: back to kind="widget" via the publish/clone unwrap, so this also skips cloned
#: widgets). ``svg`` is excluded because ``.svg`` is not in
#: ``FileReader.SUPPORTED``.
DEFAULT_AUTO_INGEST_ARTIFACT_KINDS = ["markdown", "text", "html", "json"]


def _coerce_embedding_provider(raw: str) -> str:
    """Normalize legacy or unknown embedding_provider values.

    Embeddings are always-on: every value coerces to ``"llama_cpp"``. Old configs
    may carry ``"ollama"`` (a retired runtime) or ``"none"`` (the disabled setting);
    both are transparently upgraded. Unknown values also coerce so a config file
    from a newer/older version never crashes.
    """
    return "llama_cpp"


@dataclass
class KnowledgeConfig:
    """Knowledge Library ingestion settings.

    Embedding/retrieval settings live under :class:`MemoryConfig` (shared with
    the memory subsystem via ``create_embedder_from_config``); this section
    holds Knowledge-Library-specific ingestion toggles.
    """

    auto_ingest_artifacts: bool = field(
        default=False,
        metadata=_meta(
            "Auto-Ingest Artifacts",
            "Automatically ingest content-bearing local artifacts (markdown/text "
            "documents you save and iterate) into the Knowledge Library so they "
            "become searchable, keep them in sync as the artifact changes, and "
            "remove them from the Library when the artifact is deleted. They "
            "appear as a single aggregate 'Artifacts' source. Off by default: "
            "every ingested chunk costs an LLM extraction call, so a library "
            "grows and spends only once you ask for it.",
        ),
    )
    auto_ingest_artifact_kinds: list[str] = field(
        default_factory=lambda: list(DEFAULT_AUTO_INGEST_ARTIFACT_KINDS),
        metadata=_meta(
            "Auto-Ingest Artifact Kinds",
            "Artifact kinds eligible for auto-ingest. Defaults to substantial "
            "document kinds (markdown, text, html, json); widget is excluded "
            "(UI/dashboards, not documents) and svg has no reader support.",
        ),
    )
    max_ingest_file_mb: float = field(
        default=100.0,
        metadata=_meta(
            "Max Ingest File Size (MB)",
            "Per-file size cap for Knowledge Library ingestion. Oversized files "
            "are skipped with a WARNING naming the file instead of being chunked "
            "-- chunking a very large file (e.g. a tens-of-MB CSV->MD conversion) "
            "is CPU-bound and previously hung gateway startup. Set 0 to disable "
            "the cap.",
        ),
    )
    embed_timeout_secs: float = field(
        default=10.0,
        metadata=_meta(
            "Embed Timeout (seconds)",
            "Per-request timeout for the Knowledge-Library embedder. Raise it "
            "when a large chunk times out on a cold Ollama model load (the embed "
            "then never completes and the item is retried every maintenance "
            "pass). 0 or unset keeps the built-in 10s default.",
        ),
    )
    embed_content_budget: int = field(
        default=0,
        metadata=_meta(
            "Embed Content Budget (chars)",
            "Safety bound (chars) on chunk content folded into an item embedding. "
            "0 or unset keeps the built-in default (a generous backstop for "
            "pathological un-chunked input); raise/lower only to tune truncation.",
        ),
    )
    pool_idle_ttl_secs: int = field(
        default=300,
        metadata=_meta(
            "Pool Idle TTL (secs)",
            "Seconds the document-extraction worker pool may sit fully idle "
            "before it is scaled to zero (all workers shut down, freeing ~1GB "
            "of held process trees); the next ingest respawns them lazily. "
            "0 keeps the workers warm indefinitely.",
        ),
    )
    auto_add_documents: bool = field(
        default=False,
        metadata=_meta(
            "Auto-Add Documents",
            "Let the agent add documents it comes across during normal work to the "
            "Knowledge Library, so they become searchable later. The agent reads the "
            "document with its own tools, under your approval, and hands over the "
            "text -- Kiro Crew fetches nothing itself, so the doc-ingest host "
            "allowlist below does not apply. Added documents appear in a single "
            "aggregate 'Auto-added' source you can remove in one click. Off by "
            "default: the Library should only hold what you asked it to hold. "
            "Renamed from auto_ingest_doc_links, which is still accepted.",
        ),
    )
    folder_ingest_chunk_budget: int = field(
        default=300,
        metadata=_meta(
            "Folder Ingest Chunk Budget",
            "Chunks a folder you add by hand may ingest per watcher sweep. Adding "
            "a source-code repository discovers thousands of files, and each "
            "chunk costs an LLM extraction call on a pool of billed sessions, so "
            "an unpaced first scan can spend a large amount unattended. Nothing "
            "is skipped: newest files land first and the rest continue on later "
            "sweeps. Higher than the auto-ingest budget because you asked for the "
            "folder explicitly. 0 removes the bound; a per-source chunk_budget "
            "property overrides it for one folder.",
        ),
    )
    dedup_every_n_sweeps: int = field(
        default=12,
        metadata=_meta(
            "De-duplicate Every N Sweeps",
            "Run a full duplicate-collapsing pass every Nth watcher sweep. The "
            "per-write gate refuses a byte-identical document, but only a full "
            "pass catches a near-duplicate (the same document edited slightly "
            "between two sources) or duplicates that already existed. At the "
            "default 300s sweep interval, 12 is roughly hourly. 0 disables it.",
        ),
    )
    doc_ingest_hosts: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Doc-Ingest Host Allowlist",
            "Exact hostnames whose links may be fetched by KIROCREW ITSELF and "
            "ingested, for an edition that wires a server-side doc-link scanner. "
            "Empty = fetch nothing (SSRF-safe deny-by-default). This governs only "
            "that server-fetch path -- it does NOT gate 'Auto-Add Documents' "
            "above, where the agent has already fetched the content under its own "
            "approval and Kiro Crew fetches nothing. Applying it there would make "
            "the feature ingest nothing on a default config while its toggle "
            "reads on.",
        ),
    )
    sweep_chunk_budget: int = field(
        default=500,
        metadata=_meta(
            "Global Sweep Chunk Budget",
            "Maximum chunks ingested across ALL sources in a single watcher "
            "sweep. Each chunk costs one LLM extraction call, so this is the "
            "primary global cost control. Once reached, remaining sources are "
            "deferred to the next sweep. "
            "0 removes the bound.",
        ),
    )
    import_chunk_budget: int = field(
        default=0,
        metadata=_meta(
            "Explicit Import Chunk Budget",
            "Maximum chunks ingested through the EXPLICIT one-shot import paths "
            "(a single-file add, an agent-driven add, a direct text ingest, and "
            "remote sync) within a rolling ~60s window -- the cross-file cost "
            "ceiling those paths lack, since they are many independent calls "
            "with no scan boundary the way a watcher sweep has. Each chunk costs "
            "an LLM extraction call. When the window is exhausted the next import "
            "is REFUSED with a reason rather than silently truncated, so a "
            "deliberate import never loses part of a file. A single file stays "
            "bounded by the 50-chunk per-file cap independently. 0 (the default) "
            "removes the bound -- opt in by setting it (e.g. 500, matching "
            "sweep_chunk_budget) so this changes nothing until you choose it. "
            "LIMITATION if you enable it: reservation is worst-case -- each "
            "in-flight import books the 50-chunk per-file maximum up front and "
            "reconciles to the real count only when it finishes, so several "
            "concurrent imports throttle below the nominal number you set here "
            "until that accounting is refined. Set it with that headroom in "
            "mind.",
        ),
    )
    embed_rate_limit: int = field(
        default=120,
        metadata=_meta(
            "Embedding Rate Limit (items/min)",
            "Maximum embedding generations per minute across all sources. "
            "Back-pressures the ingestion pipeline when a large backlog builds "
            "up, preventing memory/CPU saturation from parallel embed batches. "
            "0 removes the bound.",
        ),
    )
    extraction_model: str = field(
        default="",
        metadata=_meta(
            "Extraction Model",
            "LLM model used for document extraction and summarization. Empty "
            "uses the default model (agent.model). Set to a specific model id "
            "(e.g. 'claude-haiku-4.5') to use a cheaper model for extraction "
            "without changing your chat default.",
        ),
    )
    extraction_pool_size: int = field(
        default=3,
        metadata=_meta(
            "Extraction Pool Size",
            "Number of concurrent LLM workers for document extraction. More "
            "workers = faster ingestion but higher peak cost. Each worker holds "
            "a long-lived session. A change applies at the next idle boundary: "
            "the pool keeps its current width until it scales to zero, so an "
            "ingest already running is never resized under it.",
        ),
    )
    extraction_effort: str = field(
        default="",
        metadata=_meta(
            "Extraction Effort",
            "Reasoning effort for the document-extraction LLM pool. Empty "
            "runs the default high. Only applies on reasoning-capable models.",
            enum=["", *EFFORT_LEVELS],
            restart=True,
        ),
    )


def _read_auto_add_documents(knowledge_data: dict) -> bool:
    """Read the auto-add-documents toggle, honouring the older spelling.

    Accepts the older ``auto_ingest_doc_links`` spelling so an existing config's
    value carries over instead of silently reverting to the default on upgrade.
    Canonical spelling is ``auto_add_documents``, which is what ``save()`` writes,
    so a save/load round-trip settles on it.

    Absent both keys the feature is OFF: auto-ingest is opt-in, so a config that
    never mentioned it must not start adding documents.
    """
    for key in ("auto_add_documents", "auto_ingest_doc_links"):
        if key in knowledge_data:
            return bool(knowledge_data.get(key))
    return False


@dataclass
class MemoryStoreConfig:
    owner_member_id: str = field(
        default="",
        metadata=_meta("Owner Member ID", "Immutable member identity stored in this database."),
    )
    owner_member: str = field(
        default="",
        metadata=_meta(
            "Owner Member", "Display label of the Crew Member owning this memory store."
        ),
    )
    memory_version: int = field(
        default=1,
        metadata=_meta("Memory Version", "1 for existing memory; 2 for member memory."),
    )
    description: str = field(
        default="",
        metadata=_meta("Description", "Human-readable purpose of this memory store."),
    )
    embedding_provider: str = field(
        default="",
        metadata=_meta(
            "Embedding Provider",
            "Override embedding backend for this store. Empty inherits from top-level memory "
            "(embeddings are always-on; per-store disable is not supported).",
            enum=["", "llama_cpp"],
        ),
    )


@dataclass
class SkillsConfig:
    max_triggered: int = field(
        default=0,
        metadata=_meta(
            "Max Triggered",
            "Maximum number of skills a single message may flag as relevant (≥0). "
            "Each match injects that skill's full content, unless the skill sets "
            "inject_on_trigger: false (pointer-only; requires max_triggered > 0 to "
            "have any effect). Defaults to 0 (disabled): the agent discovers skills "
            "from a short name/purpose index and skill_search using short keywords. "
            "Search loads confined project bodies safely; $skillname loads a named skill. "
            "Set to a positive integer to re-enable "
            "per-turn word-overlap trigger matching.",
        ),
    )
    # ── Skill index mode ──
    lazy_load: bool = field(
        default=True,
        metadata=_meta(
            "Lazy Skill Injection",
            "When true (the default), show a bounded usage-ranked index of on-demand "
            "skills, each with its path, plus a line naming the families the index "
            "leaves out. Set to false for the shorter entry that names only the eight "
            "hottest skills and points at skill_search for the rest. Both modes use the "
            "same Crew background budget, independent of model window size, and neither "
            "applies to an agent with its own skill:// mapping, which gets a bounded "
            "directory of mapped skills with complete search, paginated listing and exact "
            "reads on demand. Required pinned instructions share an explicit startup "
            "capacity; explicit loading and trigger gates are preserved.",
        ),
    )
    # ── Auto skill creation ──
    # All fields default to OFF so upgrades are zero-impact. Enable via
    # ``kirocrew config set skills.auto_create_from_sessions true`` or the
    # dashboard Settings → Skills toggle.
    auto_create_from_sessions: bool = field(
        default=False,
        metadata=_meta(
            "Auto-Create Skills",
            "When true, analyze each session after completion and synthesize a reusable "
            "SKILL.md when the session demonstrates a recurring procedure — one a future "
            "session, working on a different target, would run again. Candidates are staged "
            "for review (see approval_required) rather than going live, and live under "
            "skills/auto/ so they never collide with hand-authored skills. Disabled by "
            "default; enable in Settings → Skills.",
        ),
    )
    auto_refine_on_deviation: bool = field(
        default=False,
        metadata=_meta(
            "Auto-Refine Skills",
            "When true, update an existing auto-created skill if the agent succeeds "
            "via a different tool sequence than documented. Requires "
            "auto_create_from_sessions. Disabled by default.",
        ),
    )
    auto_min_tool_calls: int = field(
        default=5,
        metadata=_meta(
            "Auto Min Tool Calls",
            "Minimum tool calls in a session for it to qualify for skill extraction "
            "(≥2). Lower values produce more skills but reduce quality.",
        ),
    )
    auto_similarity_threshold: float = field(
        default=0.85,
        metadata=_meta(
            "Auto Similarity Threshold",
            "Skip creation when an existing skill's description has keyword overlap "
            "≥ this fraction with the synthesized description (0.0-1.0). Prevents "
            "near-duplicate skills. Used as the lexical fallback when the Haiku "
            "dedupe judge is unavailable.",
        ),
    )
    # ── Staged approval + lifecycle (v2) ──
    approval_required: bool = field(
        default=True,
        metadata=_meta(
            "Skill Approval Required",
            "When true, auto-generated skill candidates land in a pending queue for "
            "human review instead of going live. Prose-only skills may auto-publish "
            "when this is false; skills that bundle scripts ALWAYS require approval "
            "regardless of this flag.",
        ),
    )
    max_auto_skills: int = field(
        default=100,
        metadata=_meta(
            "Max Auto Skills",
            "Hard cap (backstop) on the number of live auto-generated skills. When "
            "exceeded, the least-valuable (by recency + frequency) are archived — "
            "never hard-deleted — down to the cap (≥1).",
        ),
    )
    stale_after_days: int = field(
        default=30,
        metadata=_meta(
            "Skill Stale After (days)",
            "An auto-skill with no recorded use for this many days is marked stale "
            "(≥1). Never-used skills younger than this window are exempt (grace floor).",
        ),
    )
    archive_after_days: int = field(
        default=90,
        metadata=_meta(
            "Skill Archive After (days)",
            "An auto-skill inactive for this many days is archived (recoverable, "
            "never deleted). Must be ≥ stale_after_days.",
        ),
    )
    pending_ttl_days: int = field(
        default=30,
        metadata=_meta(
            "Pending Skill TTL (days)",
            "Unapproved skill candidates older than this are auto-cleaned from the "
            "pending queue (≥1).",
        ),
    )
    generate_scripts: bool = field(
        default=True,
        metadata=_meta(
            "Generate Skill Scripts",
            "When true, deterministic procedures may generate a validated Python "
            "helper script alongside the SKILL.md. Script-bearing skills always "
            "require approval.",
        ),
    )
    judge_model: str = field(
        default="auto",
        metadata=_meta(
            "Skill Judge Model",
            "Model used for the dedupe judge and the advisory pending review. "
            'Defaults to "auto" to inherit the account\'s governed model; the '
            "value only gates whether the judge runs (any truthy value enables "
            "it) — the judge turn itself runs on the shared background session.",
        ),
    )
    extra_paths: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Extra Skill Paths",
            "Additional directories to scan for skills. Supports ~ expansion. "
            "Skills from extra_paths are read-only (trigger matching + loading). "
            "Local ~/.kiro/crew/skills/ takes precedence for duplicate names.",
        ),
    )
    project_skills_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Project Skills",
            "Whether a chat session may load skills from its own project's "
            "<project>/.kiro/skills directory. Enabled by default, but a project's "
            "skills are still only loaded after the operator grants that specific "
            "directory trust, because a SKILL.md enters the agent's context and can "
            "instruct it to run anything. Set false to make project skills "
            "impossible regardless of any grant already recorded.",
        ),
    )

    def __post_init__(self) -> None:
        if self.max_triggered < 0:
            logger.warning("max_triggered %d < 0, using 0", self.max_triggered)
            object.__setattr__(self, "max_triggered", 0)
        if self.auto_min_tool_calls < 2:
            logger.warning("auto_min_tool_calls %d < 2, using 2", self.auto_min_tool_calls)
            object.__setattr__(self, "auto_min_tool_calls", 2)
        if not 0.0 <= self.auto_similarity_threshold <= 1.0:
            logger.warning(
                "auto_similarity_threshold %.2f out of range [0.0, 1.0], using 0.85",
                self.auto_similarity_threshold,
            )
            object.__setattr__(self, "auto_similarity_threshold", 0.85)
        if self.auto_refine_on_deviation and not self.auto_create_from_sessions:
            logger.warning(
                "auto_refine_on_deviation requires auto_create_from_sessions; "
                "disabling auto_refine_on_deviation"
            )
            object.__setattr__(self, "auto_refine_on_deviation", False)
        if self.max_auto_skills < 1:
            logger.warning("max_auto_skills %d < 1, using 1", self.max_auto_skills)
            object.__setattr__(self, "max_auto_skills", 1)
        if self.stale_after_days < 1:
            logger.warning("stale_after_days %d < 1, using 1", self.stale_after_days)
            object.__setattr__(self, "stale_after_days", 1)
        if self.archive_after_days < self.stale_after_days:
            logger.warning(
                "archive_after_days %d < stale_after_days %d, using stale_after_days",
                self.archive_after_days,
                self.stale_after_days,
            )
            object.__setattr__(self, "archive_after_days", self.stale_after_days)
        if self.pending_ttl_days < 1:
            logger.warning("pending_ttl_days %d < 1, using 1", self.pending_ttl_days)
            object.__setattr__(self, "pending_ttl_days", 1)


@dataclass
class SessionSummaryConfig:
    """Intent-level session summaries shown in the chat right panel.

    Summarizing spends tokens on a turn the user did not ask to pay for, so every
    field defaults to off/conservative and the feature is inert until ``enabled``.
    """

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Session Summaries",
            "When true, summarize each session by intent after a turn completes so "
            "the chat right panel can show what the session is about, what has "
            "happened, and what to do next. Costs tokens on turns that change the "
            "session; an unchanged session is served from cache for free. Disabled "
            "by default; enable in Settings.",
        ),
    )
    min_user_turns: int = field(
        default=2,
        metadata=_meta(
            "Minimum User Turns",
            "Skip summarization until the session has at least this many user "
            "messages (>=1). A one-exchange session has no intent structure worth "
            "extracting, and the session title already covers it.",
        ),
    )
    regenerate_after_turns: int = field(
        default=1,
        metadata=_meta(
            "Regenerate Every N Turns",
            "How many completed turns must pass before the summary is rebuilt "
            "(>=1). 1 keeps the panel current at the cost of one pass per turn; "
            "raise it to trade freshness for tokens. A cached summary whose "
            "session has not changed is never rebuilt regardless of this value.",
        ),
    )
    max_intents: int = field(
        default=50,
        metadata=_meta(
            "Maximum Intents",
            "Safety ceiling on intents stored per session (>=1). Trimming runs "
            "before the summary is saved, so whatever exceeds this is dropped "
            "from the record rather than hidden -- the panel itself withholds "
            "nothing, rendering every intent it receives and collapsing all but "
            "the most recently touched one. The ceiling therefore sits high "
            "enough that reaching it is unusual rather than routine.",
        ),
    )
    max_constraints: int = field(
        default=50,
        metadata=_meta(
            "Maximum Project Notes",
            "Safety ceiling on session-level operational notes -- the recurring facts "
            "about how this project is run (>=0). Whatever exceeds this is dropped "
            "from the record rather than hidden: how many are worth writing at all "
            "is governed by the generation prompt, and the panel bounds the expanded "
            "list's height rather than its length. Durable cross-session preferences "
            "belong in lessons rather than here.",
        ),
    )
    assistant_excerpt_chars: int = field(
        default=400,
        metadata=_meta(
            "Assistant Excerpt Size",
            "Characters kept from each end of an assistant message when building "
            "the summarization input (>=80). User messages are included in full "
            "unless the whole input exceeds the fixed 40,000-character summary input "
            "limit -- they carry intent and are small -- while assistant output is "
            "excerpted because it holds the progress detail but dominates the "
            "transcript. Past that limit, middle turns are dropped and any turn is "
            "cut to about 5,000 characters per end, so larger values stop helping.",
        ),
    )

    def __post_init__(self) -> None:
        if self.min_user_turns < 1:
            logger.warning("min_user_turns %d < 1, using 1", self.min_user_turns)
            object.__setattr__(self, "min_user_turns", 1)
        if self.regenerate_after_turns < 1:
            logger.warning("regenerate_after_turns %d < 1, using 1", self.regenerate_after_turns)
            object.__setattr__(self, "regenerate_after_turns", 1)
        if self.max_intents < 1:
            logger.warning("max_intents %d < 1, using 1", self.max_intents)
            object.__setattr__(self, "max_intents", 1)
        if self.max_constraints < 0:
            logger.warning("max_constraints %d < 0, using 0", self.max_constraints)
            object.__setattr__(self, "max_constraints", 0)
        if self.assistant_excerpt_chars < 80:
            logger.warning(
                "assistant_excerpt_chars %d < 80, using 80",
                self.assistant_excerpt_chars,
            )
            object.__setattr__(self, "assistant_excerpt_chars", 80)


def resolve_memory_store_config(
    top_level_memory: dict,
    store_overrides: dict,
) -> dict:
    """Deep-merge store overrides onto top-level memory defaults.

    Merge happens at the raw dict level BEFORE dataclass construction.
    A store that only sets embedding_provider inherits all other memory
    settings from the top-level config, not from MemoryConfig defaults.
    """
    merged = dict(top_level_memory)
    for key, value in store_overrides.items():
        if key == "description":
            continue  # description is store-only metadata, not a memory setting
        if value != "" and value is not None:
            merged[key] = value
    return merged
