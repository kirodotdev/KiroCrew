/**
 * Host-side backup for the dashboard settings that live in `localStorage`.
 *
 * The problem this solves: most settings the user sets in the UI are stored
 * only in the renderer's `localStorage`, and `localStorage` is keyed by ORIGIN
 * and (in the desktop app) kept inside Electron's `userData` directory. Both
 * can change without the user doing anything: the dashboard port moves, the
 * `userData` directory is relocated by a package rename, the user switches
 * between the stable and nightly builds, or the browser evicts the origin. Each
 * of those looks identical from the user's chair — "I upgraded and had to set
 * everything up again" — even though nothing on the host was lost, because
 * nothing on the host was ever written.
 *
 * So: mirror the durable subset to the gateway (`/api/ui-prefs`, stored in the
 * data home), and read it back when a profile shows up with none of those keys.
 *
 * Design decisions worth knowing before changing this file:
 *
 * 1. **Read on a COLD profile only.** The server copy is a backup, not a live
 *    cross-tab sync channel. If any durable key exists locally, that copy wins
 *    and the server is only written to. Reading on every boot would let a stale
 *    backup fight a tab the user just changed, and would make two browsers on
 *    one host tug settings back and forth.
 * 2. **Snapshot-diff, not write interception.** ~300 call sites write
 *    `localStorage` directly. Rather than route them all through one writer (a
 *    migration that would go stale the next time someone adds a raw call), this
 *    module periodically reads the allowlisted keys and PUTs what changed. Any
 *    writer, present or future, is covered.
 * 3. **Explicit allowlist, no prefix wildcards for session-scoped keys.**
 *    Ephemeral and per-session state (height caches, panel tabs, drafts,
 *    touched files, web-preview URLs) is deliberately excluded: it is worthless
 *    on another origin, it is what the quota reclaimer already evicts, and
 *    mirroring it would grow without bound.
 * 4. **Keys already mirrored server-side are excluded** — theme mode, theme
 *    colour, language and the onboarding flags reconcile through
 *    `/api/config/theme` and `dashboard.*` config. Backing them up here too
 *    would create a second source of truth for the same setting.
 */

import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { bottomTerminalPrefsSnapshot } from '../hooks/useBottomTerminal'

/** Terminal labels have per-id write coordinates, but share the layout's
 *  existing backup key. Projection is read-only: syncing cannot race a layout
 *  mutation by publishing a synthesized snapshot back into localStorage. */
function readPreference(key: string): string | null {
  const raw = safeGetItem(key)
  return key === 'mc-bottom-terminal' && raw !== null ? bottomTerminalPrefsSnapshot(raw) : raw
}

/**
 * The one durable key whose VALUE is a JSON object of independent settings,
 * synced one FIELD at a time rather than as a single blob.
 *
 * `mc-chat-config` holds ~20 settings a user sets at different times across
 * different origins (the phone over the tunnel, the macOS app, a desktop
 * browser). Synced as one opaque string, a profile that changes any ONE of them
 * uploads the whole blob -- including its own, possibly stale, copy of every
 * other field -- and the host (which merges per localStorage KEY, not per field)
 * replaces the stored blob wholesale. So a second origin holding older Pin or
 * Compact values overwrites the host copy of settings it never touched, and the
 * phone's next cold restore brings those stale values back (issue #15236).
 *
 * Projecting each field onto its own wire key makes the host's existing per-key
 * merge a per-FIELD merge: a profile uploads only the fields it actually
 * changed, so it never writes a field it did not touch. The server stays opaque
 * -- it never parses the value, it just has more, smaller keys. The split lives
 * entirely in the projection helpers below; `buildPatch`, the per-key retry,
 * fingerprints, deletion and the growth-gap reconcile all operate on the child
 * keys as ordinary durable keys.
 *
 * One composite exists today. Sibling blobs (`mc-notification-sound`,
 * `kc:file-explorer:state:v2`) share the shape and could join, but this change
 * ships only the key the issue is about; the helpers take the single key
 * directly rather than iterating a registry built for members that do not exist.
 */
const COMPOSITE_KEY = 'mc-chat-config'

/**
 * Wire key for one field of the composite: `mc-chat-config.<hex(field)>`.
 *
 * The field name is hex-encoded, not written raw, for two reasons the raw form
 * got wrong:
 *   * the server refuses any key whose name contains `token`, `secret`, ... as
 *     a credential guard (`DENY_SUBSTRINGS` in `ui_prefs.py`), and
 *     `showContextTokens` -- a real `ChatConfig` field present in every saved
 *     blob -- contains `token`, so a raw wire key `mc-chat-config.showContextTokens`
 *     is rejected and the whole patch 400s on every flush;
 *   * a raw field could in principle contain the `.` separator and split
 *     ambiguously.
 * Hex (`[0-9a-f]`) provably contains none of the denied substrings and no
 * separator character, and reverses exactly, so every field name is safe on the
 * wire regardless of what a future `ChatConfig` field is called.
 */
const COMPOSITE_SEP = '.'
const COMPOSITE_PREFIX = `${COMPOSITE_KEY}${COMPOSITE_SEP}`

function encodeField(field: string): string {
  let hex = ''
  for (let i = 0; i < field.length; i++) {
    hex += field.charCodeAt(i).toString(16).padStart(4, '0')
  }
  return hex
}

function decodeField(hex: string): string | null {
  if (hex.length === 0 || hex.length % 4 !== 0 || !/^[0-9a-f]+$/.test(hex)) return null
  let out = ''
  for (let i = 0; i < hex.length; i += 4) {
    out += String.fromCharCode(parseInt(hex.slice(i, i + 4), 16))
  }
  return out
}

function childKey(field: string): string {
  return `${COMPOSITE_PREFIX}${encodeField(field)}`
}

/** The field name behind a composite child wire key, or null when the key is
 *  not a well-formed composite child. */
function childField(wireKey: string): string | null {
  if (!wireKey.startsWith(COMPOSITE_PREFIX)) return null
  return decodeField(wireKey.slice(COMPOSITE_PREFIX.length))
}

/** A child whose stored value is not valid JSON. Distinct from JSON `null` (a
 *  legitimate field value) so a malformed child is skipped rather than written
 *  as null over a valid local field. */
const PARSE_FAILED = Symbol('parse-failed')

/** Parse a composite child's JSON field value. A JSON `null` round-trips as
 *  `null` (a real field value); an unparseable value returns `PARSE_FAILED` so
 *  the caller can skip it instead of clobbering a valid local field with null. */
function safeParse(value: string): unknown {
  try {
    return JSON.parse(value)
  } catch {
    return PARSE_FAILED
  }
}

/**
 * Expand the composite's stored blob into `{childKey: fieldJson}`.
 *
 * Each field value is re-serialized on its own, so a boolean, number or string
 * field fingerprints and diffs independently of its siblings. A blob that is
 * absent, unparseable, or not a JSON object yields no children -- the composite
 * is simply not represented on the wire, exactly as a missing plain key is.
 */
function expandComposite(): Map<string, string> {
  const out = new Map<string, string>()
  const raw = readPreference(COMPOSITE_KEY)
  if (raw === null) return out
  let blob: unknown
  try {
    blob = JSON.parse(raw)
  } catch {
    return out
  }
  if (!blob || typeof blob !== 'object' || Array.isArray(blob)) return out
  for (const [field, value] of Object.entries(blob as Record<string, unknown>)) {
    // Serialize each field on its own and SKIP any that throws (GPT 6.1 F1): a
    // legacy backup could hold a value nested thousands of arrays deep (within
    // the store's size cap but past the JS stack), so a bare JSON.stringify
    // here raises an uncaught RangeError inside hasUnreconciledKeys() -- before
    // React mounts -- crashing the dashboard on boot with no recovery. Dropping
    // only the unserializable field leaves every valid sibling fingerprinted
    // and syncing; the bad field is simply not represented on the wire, exactly
    // as an absent key, and remains readable from the raw blob by local readers.
    let json: string
    try {
      json = JSON.stringify(value)
    } catch {
      continue
    }
    out.set(childKey(field), json)
  }
  return out
}

/** Write one restored field into the composite blob in localStorage, merging it
 *  with whatever the blob already holds. Returns false when the quota-safe
 *  writer had to drop it. */
function writeCompositeField(field: string, value: unknown): boolean {
  const raw = readPreference(COMPOSITE_KEY)
  let blob: Record<string, unknown> = {}
  if (raw !== null) {
    try {
      const parsed: unknown = JSON.parse(raw)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        blob = parsed as Record<string, unknown>
      }
    } catch {
      /* unparseable local blob: rebuild from the restored field alone */
    }
  }
  blob[field] = value
  return safeSetItem(COMPOSITE_KEY, JSON.stringify(blob))
}

/**
 * True when a wire key is one this build knows how to sync: a plain durable key
 * that is NOT the composite parent (the parent never travels whole), or a
 * well-formed composite child. It gates which stored fingerprints are admitted
 * -- a child's hex name is not in the static allowlist, so a plain membership
 * test would discard its baseline and re-upload it every boot.
 */
function isWireKey(wireKey: string): boolean {
  if (DURABLE_PREF_KEYS.includes(wireKey)) return wireKey !== COMPOSITE_KEY
  return childField(wireKey) !== null
}

/**
 * One-time migration for a host backup written by a build that stored the
 * composite as ONE whole-blob key. Expand that blob into the per-field child
 * keys this build restores from, so an existing user's chat settings are not
 * lost on the first cold load after the upgrade. A field the host already holds
 * as a child key wins over the same field inside the legacy blob -- it is the
 * per-field write, which only a build with this fix produces and which a flush
 * keeps current -- so re-running this never resurrects a field a later per-field
 * flush deleted. Returns a shallow copy only when something was added; the
 * original object otherwise.
 *
 * Accepted tradeoff in the stable/nightly mixed-version case: the two builds
 * have separate origins (see this module's header), so once the fixed build
 * writes a field as a child key it is the live source for that field; an old
 * build that keeps rewriting the legacy whole-blob never overrides a child key
 * through this path. A field present ONLY in the legacy blob (one the fixed
 * build never wrote) still restores from it, which is the behaviour an
 * all-old-build user already has. Neither copy is retired here -- that is more
 * machinery than issue #15236 needs, and the per-field keys are authoritative
 * the moment any fixed build touches the field.
 */
function withLegacyCompositeExpanded(
  hostValues: Record<string, unknown>,
): Record<string, unknown> {
  let out: Record<string, unknown> | null = null
  const blob = hostValues[COMPOSITE_KEY]
  if (typeof blob !== 'string') return hostValues
  let parsed: unknown
  try {
    parsed = JSON.parse(blob)
  } catch {
    return hostValues
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return hostValues
  for (const [field, value] of Object.entries(parsed as Record<string, unknown>)) {
    const ck = childKey(field)
    if (ck in hostValues) continue // a per-field write already supersedes it
    // Serialize each field on its own and SKIP any that throws (GPT 6.1 F1): a
    // legacy backup could hold a value nested thousands of arrays deep (within
    // the store's size cap but past the JS stack), so a bare JSON.stringify
    // here raises an uncaught RangeError during the pre-mount reconcile --
    // crashing the dashboard on boot with no recovery. Dropping only the
    // unserializable field restores every valid sibling; the bad field is
    // simply absent on the wire and still readable from the raw blob locally.
    let json: string
    try {
      json = JSON.stringify(value)
    } catch {
      continue
    }
    out ??= { ...hostValues }
    out[ck] = json
  }
  return out ?? hostValues
}

/**
 * Durable, user-chosen preferences. Exact keys only.
 *
 * A key belongs here when a user would be annoyed to set it twice, and would
 * NOT be surprised to see it follow them to a new window. When in doubt leave
 * it out: a missing backup degrades to today's behaviour, while backing up
 * session-scoped state resurrects stale UI on an unrelated profile.
 *
 * Growing this list is safe ONLY because of the reconcile pass. A warm profile
 * never cold-hydrates (`needsHydrate` is false once `SYNCED_KEYS_KEY` exists),
 * so without it a newly added key's local DEFAULT -- often written by a hook on
 * mount -- would be flushed to the host before the host's own value was ever
 * read, overwriting a value another origin saved (growth-gap issue 9491).
 * `reconcileNewDurableKeys` runs before the first flush and reads the host copy
 * for keys this profile has never synced: an absent local key adopts the host
 * value, a present one is baselined so the first flush does not upload it. A
 * key counts as "new" when it is in neither the synced fingerprints nor the
 * reconciled roster (see `ROSTER_ENTRY`), so the pass costs one GET per
 * allowlist growth, not per boot.
 */
export const DURABLE_PREF_KEYS: readonly string[] = [
  // Chat preferences — one JSON blob holding ~16 settings (send-key mode,
  // timestamps, turn stats, content width, collapse-steps, stream mode, ...).
  // This single key is the most-reported loss; see issue #8705.
  'mc-chat-config',
  'mc-busy-send-mode',
  // Typography and zoom.
  //
  // NOT here: 'mc-zoom' and 'mc-font-scale'. hooks/useZoom.ts runs a one-time
  // migration that folds both legacy page-scaling keys into the native zoom
  // factor and then DELETES them, so backing them up would restore them on the
  // next cold profile and re-run the migration forever.
  'mc-font-family',
  // The Custom Font Family choice: the picked family name and the ligature
  // toggle. 'mc-font-family' above already persists that Custom is SELECTED, so
  // without these two a cold profile restores family=custom while the chosen
  // font and ligature preference silently revert to the Sans fallback / default
  // on. Carried by the same reconcile growth path documented above.
  'mc-custom-font',
  'mc-custom-font-ligatures',
  // Chat reading width (md | full) -- hooks/useReadingWidth.ts.
  'mc-reading-width',
  // Navigation and shell layout the user arranged by hand.
  'mc-nav',
  // Interface paradigm (chat | cli) -- hooks/useUIMode.tsx. Its provider
  // persists the current mode on MOUNT, so on a warm origin this key is always
  // present locally; the reconcile pass below is what keeps that mount-written
  // default from overwriting another origin's backup.
  'mc-ui',
  'mc-app-nav-order',
  'mc-apps-expanded',
  'mc-bottom-terminal',
  'mc-files-rail-open',
  'mc-crews-view',
  'mc-crew-switcher-pinned',
  'mc-crew-switcher-stable-order',
  'mc-filter-folders-shelved',
  'mc-flat-hidden-folders',
  'mc-input-height',
  // Diff and file viewer toggles.
  'mc-diff-plain',
  'mc-diff-split',
  'mc-file-linenums',
  'mc-file-wordwrap',
  'mc-file-collapse-unchanged',
  // Artifacts browser.
  'mc-artifacts-view',
  'mc-artifacts-sort',
  'mc-artifacts-pinned-only',
  'mc-artifacts-session-docs-collapsed',
  'mc-artifact-folders-expanded',
  // Misc opt-ins and acknowledgements the user should not have to repeat.
  //
  // NOT here: 'mc-yolo-ack'. Its mere PRESENCE makes ApprovalModePicker skip the
  // confirmation dialog and activate full auto-approval directly, and this backup
  // lives in the agent-writable data home — so restoring it would let an agent
  // that can write ~/.kiro/crew/ui-prefs.json pre-satisfy a human safety gate on
  // the user's next fresh origin. The rule this key is an instance of: a value
  // that GATES a safety confirmation must not be restorable from a file the agent
  // can write, however convenient re-acknowledging is.
  'mc-dev-mode',
  'mc-agent-scene',
  'mc-kb-graph-physics',
  // Notification sound settings (enabled/volume/per-category presets) --
  // hooks/useNotificationSound.ts. JSON blob, written only on an explicit save.
  'mc-notification-sound',
  'mc:notif:activeKinds:v2',
  'kirocrew:account-email-hidden',
  'kirocrew:comment-hint-dismissed',
  // Cloud launch defaults.
  'mc-cloud-profile',
  'mc-cloud-region',
  'mc-cloud-size',
  // Per-app preferences.
  'kc-cron-folders-collapsed',
  'kc:file-explorer:state:v2',
  'kc:issue-radar:ui-state',
  'telemetry:tab',
  'telemetry:spend-group',
  'mdnb-view',
  'mdnb-sort',
  'mdnb-list-view',
  'mdnb-full-width',
  'mdnb-panel-width',
  'mdnb-panel-open',
  'mdnb-auto-commit',
  'mdnb-auto-sync',
  'mdnb-auto-sync-mins',
  'mdnb-sync-shortcut',
  'ste_rail_w',
]

/**
 * The allowlist as it stood BEFORE the reconcile mechanism shipped, frozen
 * forever -- never append to this list; new keys go in `DURABLE_PREF_KEYS`
 * only.
 *
 * This is the reconcile baseline for a profile that carries no roster entry: a
 * legacy profile predates the roster, and every key its builds ever knew is in
 * this list, so "durable now but not in here" is exactly "added after this
 * profile could have known it". Without the frozen baseline, "no fingerprint"
 * had to stand in for "newly added", and the two are not the same: a key the
 * profile simply NEVER HELD (most of them, for most users) has no fingerprint
 * either, and reconciling those would bulk-import another origin's layout onto
 * a warm profile -- the cross-origin sync that design decision 1 above rules
 * out.
 */
const PRE_ROSTER_KEYS: readonly string[] = [
  'mc-chat-config',
  'mc-busy-send-mode',
  'mc-font-family',
  'mc-nav',
  'mc-app-nav-order',
  'mc-apps-expanded',
  'mc-bottom-terminal',
  'mc-files-rail-open',
  'mc-crews-view',
  'mc-crew-switcher-pinned',
  'mc-crew-switcher-stable-order',
  'mc-filter-folders-shelved',
  'mc-flat-hidden-folders',
  'mc-input-height',
  'mc-diff-plain',
  'mc-diff-split',
  'mc-file-linenums',
  'mc-file-wordwrap',
  'mc-file-collapse-unchanged',
  'mc-artifacts-view',
  'mc-artifacts-sort',
  'mc-artifacts-pinned-only',
  'mc-artifacts-session-docs-collapsed',
  'mc-artifact-folders-expanded',
  'mc-dev-mode',
  'mc-agent-scene',
  'mc-kb-graph-physics',
  'mc:notif:activeKinds:v2',
  'kirocrew:account-email-hidden',
  'kirocrew:comment-hint-dismissed',
  'mc-cloud-profile',
  'mc-cloud-region',
  'mc-cloud-size',
  'kc-cron-folders-collapsed',
  'kc:file-explorer:state:v2',
  'kc:issue-radar:ui-state',
  'telemetry:tab',
  'telemetry:spend-group',
  'mdnb-view',
  'mdnb-sort',
  'mdnb-list-view',
  'mdnb-full-width',
  'mdnb-panel-width',
  'mdnb-panel-open',
  'mdnb-auto-commit',
  'mdnb-auto-sync',
  'mdnb-auto-sync-mins',
  'mdnb-sync-shortcut',
  'ste_rail_w',
]

const ENDPOINT = '/api/ui-prefs'
/**
 * Fingerprints of the durable keys this profile last successfully synced with
 * the host, as `{key: hash}`.
 *
 * Three jobs, all of which need to outlive a reload:
 *  1. Change detection after a reload. `lastSent` is in-memory, so the first
 *     flush of a new page had no baseline and re-PUT every local value — which
 *     on a profile holding STALE values overwrote newer preferences another
 *     origin had already backed up. Fingerprints make that first flush send only
 *     what this profile actually changed.
 *  2. Deletion detection. Without a persisted key set, a key this profile once
 *     uploaded and has since deleted was never reported, and a later cold
 *     profile restored the value the user had deleted.
 *  3. "Have we ever reached the host?" Its absence is what makes the boot-time
 *     hydrate retry. Without it, one failed fetch on a fresh profile burned the
 *     only restore attempt the backup exists for, because the first setting
 *     written afterwards made the profile look warm forever.
 *
 * Hashes rather than values: one of these keys can hold a large blob, and storing
 * the values would double their footprint in the very storage this feature exists
 * to protect. The hash only has to detect CHANGE, so a non-cryptographic string
 * hash is the right tool — nothing here is a security decision.
 *
 * Bookkeeping, so deliberately NOT in DURABLE_PREF_KEYS: it describes this
 * profile's relationship to the host, not a user preference.
 */
const SYNCED_KEYS_KEY = 'mc-ui-prefs-synced'
/**
 * Written when a never-synced profile's restore FAILED (network, 401/403, 5xx).
 * Its value is the JSON list of durable keys the profile ALREADY HELD at that
 * moment. It changes what the NEXT successful restore does with a key that is
 * present both locally and on the host:
 *
 *   * a key in the list was the user's before anything went wrong, and any
 *     change they made since is theirs too — local wins, as always;
 *   * a key NOT in the list was written after the failure by a page that
 *     rendered without its settings (a login screen counts): hooks that persist
 *     on mount wrote defaults into localStorage. Treating those as the user's
 *     choice would make the first flush upload defaults over the real backup —
 *     the very thing the restore failed to read. For those keys the host wins.
 *
 * Letting the host win for EVERY key instead (an earlier version did) had the
 * mirror-image defect: a returning user whose GET failed once, then changed a
 * preference, saw the stale host value overwrite the change at next launch.
 *
 * A repeat failure never widens the list — the keys added since the first
 * failure are exactly the untrusted ones. Cleared by the first successful
 * restore. A warm profile upgrading into this feature cannot be caught by it:
 * its first GET finds an empty host, succeeds, and there is nothing to override.
 */
const HYDRATE_PENDING_KEY = 'mc-ui-prefs-hydrate-pending'
/**
 * Reserved entry INSIDE the `SYNCED_KEYS_KEY` document holding the durable-key
 * roster this profile last reconciled, as a JSON string array. It can never be
 * mistaken for a fingerprint: `readSyncedPrints` only admits keys in
 * `DURABLE_PREF_KEYS`, and the allowlist test pins this name out of that list.
 *
 * A key in `DURABLE_PREF_KEYS` but in neither this roster nor the synced
 * fingerprints was added to the allowlist AFTER this profile last talked to
 * the host, and `reconcileNewDurableKeys` must read the host's copy of it
 * before the first flush may run (see the allowlist doc). A profile with no
 * roster predates the mechanism, so its baseline is the frozen
 * `PRE_ROSTER_KEYS` -- every key its builds could have known.
 *
 * Living inside the synced document is what makes a DOWNGRADE shed it: a
 * pre-roster build's `readSyncedPrints` ignores the entry and its next
 * `commitSent` rewrites the document without it, so after a re-upgrade the
 * key is reconciled AGAIN instead of trusted from a stale roster -- the old
 * build also dropped the key's fingerprint, and roster-without-fingerprint
 * would upload the stale local value over a backup another origin refreshed
 * meanwhile. A separate localStorage key could not have this property: no
 * shipped build would ever rewrite or delete it.
 */
const ROSTER_ENTRY = 'mc:ui-prefs:roster'

/** Keys the profile held when its first restore failed, or null if it never failed. */
function ownedAtFailure(): Set<string> | null {
  const raw = safeGetItem(HYDRATE_PENDING_KEY)
  if (raw === null) return null
  try {
    return new Set((JSON.parse(raw) as string[]).filter((k) => typeof k === 'string'))
  } catch {
    return new Set()
  }
}

/**
 * Whether a composite CHILD wire key was owned at the failure that produced
 * `owned`.
 *
 * A current-build marker records one entry per field the profile ACTUALLY held
 * at the failure, plus the parent key as a downgrade compat signal. So the
 * authoritative per-field answer is the exact child key -- NOT the parent. An
 * earlier version read `owned.has(child) || owned.has(parent)` as a plain OR,
 * which let the parent grant claim fields that did not exist at the failure: a
 * failed restore on a PARTIAL blob, followed by a full `saveChatConfig` filling
 * ~20 `DEFAULTS` the user never chose, then had every one of those defaults
 * counted as owned and kept over the host's real values (GPT 5.6 / Opus 5,
 * residual/crash-data-loss-corruption).
 *
 * The parent grant is honoured ONLY for a legacy marker that carries the parent
 * but NO child entries -- the shape a pre-split build writes, where per-field
 * ownership is genuinely unavailable and the whole blob is the finest grain
 * there is. When any child entry is present the marker is from a split-aware
 * build and the exact child key is required.
 */
function compositeChildOwned(owned: Set<string>, childWireKey: string): boolean {
  if (owned.has(childWireKey)) return true
  const hasChildEntry = [...owned].some((k) => childField(k) !== null)
  return !hasChildEntry && owned.has(COMPOSITE_KEY)
}

/** Record a failed restore. Never widens an existing snapshot (see the key's doc). */
function markHydrateFailed(): void {
  if (safeGetItem(HYDRATE_PENDING_KEY) !== null) return
  // Wire-key granularity: a composite parent contributes one entry per field it
  // currently holds, so the owned-at-failure rule can trust or distrust each
  // chat setting independently -- the same split the restore and flush use.
  const owned = [...readLocalSnapshot().keys()]
  // Also record the composite PARENT key when any of its children is owned. A
  // DOWNGRADE to a pre-split build reads ownership by iterating the whole-blob
  // DURABLE_PREF_KEYS, so it looks up the parent `mc-chat-config`, never the
  // hex child keys; without the parent entry it would find the parent "not
  // owned" and let the stale host blob overwrite the user's local chat settings
  // (GPT 5.6, residual/crash-data-loss-corruption). The current reader does NOT
  // treat the parent as a blanket grant over its children -- `compositeChildOwned`
  // honours the parent only on a legacy marker carrying no child entries, so on
  // THIS marker (which has child entries) the parent is consulted by the old
  // build alone and never lets a post-failure default masquerade as owned.
  if (owned.some((k) => childField(k) !== null) && !owned.includes(COMPOSITE_KEY)) {
    owned.push(COMPOSITE_KEY)
  }
  safeSetItem(HYDRATE_PENDING_KEY, JSON.stringify(owned))
}

/** The raw roster entry in the synced document, or null when absent/unreadable. */
function currentRosterEntry(): string | null {
  const raw = safeGetItem(SYNCED_KEYS_KEY)
  if (raw === null) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    const entry = (parsed as Record<string, unknown>)[ROSTER_ENTRY]
    return typeof entry === 'string' ? entry : null
  } catch {
    return null
  }
}

/** The roster this profile last reconciled, or null when it predates the
 * mechanism (or the entry is unreadable) -- the caller then falls back to the
 * frozen `PRE_ROSTER_KEYS`, which can only ever treat MORE keys as new, never
 * import ones the profile might have chosen not to sync.
 */
function readRoster(): Set<string> | null {
  const entry = currentRosterEntry()
  if (entry === null) return null
  try {
    const list: unknown = JSON.parse(entry)
    if (!Array.isArray(list)) return null
    return new Set(list.filter((k): k is string => typeof k === 'string'))
  } catch {
    return null
  }
}

/**
 * Composite fields the USER explicitly edited, held as a JSON list of field
 * names in its own durable key.
 *
 * Why this exists: a composite child is withheld from the flush while it is
 * unreconciled (no fingerprint, no roster entry) so a default a hook mounts is
 * never uploaded over another origin's host value. But that same withholding
 * also drops a value the user DELIBERATELY set to its default -- a brand-new
 * field another origin seeded, that the user then edits locally back to the
 * default -- because "equals the default" and "has no fingerprint" are
 * indistinguishable from the stored blob alone (`loadChatConfig` fills every
 * field with its default, so the blob never records which were chosen). The
 * reconcile then baselines it without uploading, and a later storage reset
 * restores the stale host value over the user's choice (GPT 5.6 F1, :697).
 *
 * The fix is a marker, not write interception: `saveChatConfig` is the single
 * seam every chat-config edit already passes through, and it alone knows which
 * fields changed. It records the changed field names here, and a dirty child is
 * forced into the upload even when it equals the known default and carries no
 * fingerprint. The marker is cleared per field the moment that field's value is
 * successfully flushed (`commitSent`), so it never forces a second upload.
 */
const COMPOSITE_DIRTY_KEY = 'mc-chat-config-dirty'

/** The user-edited composite fields, as a set of field names. */
function readDirtyFields(): Set<string> {
  const raw = safeGetItem(COMPOSITE_DIRTY_KEY)
  if (raw === null) return new Set()
  try {
    const list: unknown = JSON.parse(raw)
    if (!Array.isArray(list)) return new Set()
    return new Set(list.filter((f): f is string => typeof f === 'string'))
  } catch {
    return new Set()
  }
}

function writeDirtyFields(fields: Set<string>): boolean {
  if (fields.size === 0) {
    localStorage.removeItem(COMPOSITE_DIRTY_KEY)
    return true
  }
  return safeSetItem(COMPOSITE_DIRTY_KEY, JSON.stringify([...fields]))
}

/**
 * Record that the user explicitly edited these composite fields, so their
 * values upload even when equal to the known default. Called from the
 * `saveChatConfig` edit seam -- the one place a chat-config write happens -- so
 * this is a marker set at an existing seam, NOT the per-writer interception
 * design decision 2 rules out.
 *
 * Returns whether the marker was persisted. The caller MUST treat a false here
 * as the save not having completed consistently (an edited field recorded in
 * the blob but not the marker is withheld and baselined without upload, so a
 * cold restore reinstates the stale host value -- GPT 5.6 F2): roll the blob
 * back rather than leave a stored-but-unmarked edit.
 */
export function markCompositeFieldsDirty(fields: readonly string[]): boolean {
  if (fields.length === 0) return true
  const dirty = readDirtyFields()
  for (const f of fields) dirty.add(f)
  return writeDirtyFields(dirty)
}

/** The wire keys of the user-edited composite fields (dirty set projected onto
 *  child keys), for forcing them into the upload. */
function dirtyChildKeys(): Set<string> {
  const out = new Set<string>()
  for (const f of readDirtyFields()) out.add(childKey(f))
  return out
}

/** Drop the fields whose child wire keys were just flushed, so a dirty marker
 *  forces exactly one upload and does not re-fire next flush. */
function clearDirtyChildren(sentWireKeys: Iterable<string>): void {
  const dirty = readDirtyFields()
  if (dirty.size === 0) return
  let changed = false
  for (const wireKey of sentWireKeys) {
    const field = childField(wireKey)
    if (field !== null && dirty.delete(field)) changed = true
  }
  if (changed) writeDirtyFields(dirty)
}

/** Record the CURRENT build's roster inside the synced document. */
function markReconciled(): void {
  // Carry forward composite children a prior reconcile recorded. A child whose
  // host value was absent cleared via its roster entry alone (it has no
  // fingerprint), so dropping it here would make it read as unreconciled again
  // on the next boot (Opus 5, :634). Plain keys live in the static
  // DURABLE_PREF_KEYS and need no carry-forward.
  const priorChildren = [...(readRoster() ?? new Set<string>())].filter(
    (k) => childField(k) !== null,
  )
  const rosterList = [...DURABLE_PREF_KEYS, ...priorChildren]
  const out: Record<string, unknown> = { [ROSTER_ENTRY]: JSON.stringify(rosterList) }
  const prints = readSyncedPrints()
  for (const [k, v] of prints) out[k] = v
  safeSetItem(SYNCED_KEYS_KEY, JSON.stringify(out))
}

/**
 * True when the RAW synced document carries a fingerprint under the composite
 * PARENT key -- the signature of a profile that synced `mc-chat-config` as one
 * whole blob under a build from before the per-field split. `readSyncedPrints`
 * drops that fingerprint (the parent is not a wire key), so it is read directly
 * here. It never fires on a fresh profile, which has no fingerprint at all, so
 * it distinguishes "warm profile upgrading from the whole-blob build" (its
 * children must be reconciled from the host before the first flush) from "fresh
 * profile seeding the backup" (its children simply flush).
 */
function legacyCompositeSynced(): boolean {
  const raw = safeGetItem(SYNCED_KEYS_KEY)
  if (raw === null) return false
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return false
    return typeof (parsed as Record<string, unknown>)[COMPOSITE_KEY] === 'string'
  } catch {
    return false
  }
}

/**
 * Durable keys this profile has neither synced nor reconciled -- the ones a
 * build upgrade just added to the allowlist, PLUS the composite's child wire
 * keys when the profile is upgrading from the whole-blob build. In the synced
 * fingerprints means the profile has exchanged the key with the host; in the
 * roster means a reconcile already read the host's copy and decided. Either
 * clears a plain key. NOT "no fingerprint" alone: a key the profile simply
 * never held has no fingerprint either, and treating those as new would
 * bulk-import another origin's values onto a warm profile (the cross-origin
 * sync design decision 1 rules out).
 *
 * The composite's children are the growth-gap case made concrete: on a legacy
 * upgrade they are brand-new wire keys with no fingerprints, so without
 * reconciliation the first warm flush would read every one as "changed" and
 * upload this origin's whole stale chat config over another origin's newer
 * per-field host values -- the very clobber this change exists to stop. They
 * are reported unreconciled only while a legacy whole-blob fingerprint is still
 * present (the upgrade is unfinished) and the child has no fingerprint of its
 * own yet; the reconcile pass clears both.
 */
function unreconciledKeys(): string[] {
  const prints = readSyncedPrints()
  const known = readRoster() ?? new Set(PRE_ROSTER_KEYS)
  const out = DURABLE_PREF_KEYS.filter(
    (k) => k !== COMPOSITE_KEY && !prints.has(k) && !known.has(k),
  )
  // A composite child is unreconciled when it has neither a fingerprint NOR a
  // roster entry -- the SAME two-sided clearing a plain key gets (see the
  // allowlist and ROSTER_ENTRY docs). A fingerprint means the child was synced;
  // a roster entry means a reconcile already read the host's copy and decided
  // (adopt / keep-and-baseline / host-has-nothing-so-seed-later). Earlier this
  // trigger keyed on a child FINGERPRINT alone, but the host-has-nothing branch
  // of the reconcile never writes a fingerprint, so such a child stayed
  // "unreconciled" forever: it was withheld from every flush yet `commitSent`
  // fingerprinted it from a value never sent, and the user's choice was silently
  // never backed up (Opus 5). The roster entry is written for every child the
  // pass reconciled regardless of outcome, so it clears exactly once.
  //
  // The TRIGGER (should the composite be reconciled at all?) fires only when the
  // profile has already exchanged the composite with the host in some form --
  // either a legacy whole-blob fingerprint (upgrading from the pre-split build)
  // or at least one reconciled/synced child (a later release added a field the
  // existing baseline predates). A profile that has NEVER touched the composite
  // is excluded: there a fresh child is a genuine first seed that must flush,
  // not be withheld (cross-origin design decision 1). Crucially the trigger does
  // NOT key on `hasChildPrint` unconditionally -- that condition nothing clears,
  // which routed every warm boot through a reconcile GET and turned a transient
  // GET failure into a whole session with no backup (GPT 5.6). Once the children
  // are reconciled they carry roster entries, the unreconciled set is empty, and
  // the steady-state boot pays no reconcile GET.
  const compositeTouched =
    legacyCompositeSynced() ||
    [...prints.keys()].some((k) => childField(k) !== null) ||
    [...known].some((k) => childField(k) !== null)
  if (compositeTouched) {
    // While a legacy whole-blob fingerprint is still present (the upgrade is
    // unfinished), a missing child fingerprint ALONE marks the child unresolved
    // -- the same authoritative signal `reconcileNewDurableKeys` uses via
    // `!prints0.has(k)`. The roster entry cannot be trusted here: a pre-split
    // tab left open across the upgrade flushes, and its `writeSyncedPrints`
    // rebuilds the synced doc from its own `current` (parent blob, no hex
    // children), shedding every child fingerprint while this tab's reconcile has
    // already written the roster. If we also required `!known.has(child)`, those
    // shed children would read as reconciled, `withheld` would hold only the
    // parent, and the next flush would re-upload this origin's whole stale blob
    // over another origin's per-field backup -- the #15236 clobber this change
    // removes (Opus 5). Once the legacy fingerprint is gone (migration done) the
    // roster is the authoritative clearing signal again.
    const legacy = legacyCompositeSynced()
    const unresolved = [...expandComposite().keys()].filter(
      (child) => !prints.has(child) && (legacy || !known.has(child)),
    )
    // Emit the parent key as a trigger ONLY while something is genuinely
    // unresolved (a legacy blob to migrate, or a child lacking both a
    // fingerprint and a roster entry). A fully reconciled profile reports an
    // empty set here, so `hasUnreconciledKeys()` is false and no boot reconcile
    // runs. The parent marker lets the pass run even with zero local children
    // (the reconcile loop reconciles the union of local and host children).
    if (legacy || unresolved.length > 0) {
      out.push(COMPOSITE_KEY)
      out.push(...unresolved)
    }
  }
  return out
}

/**
 * True when this WARM profile must reconcile newly added durable keys with the
 * host before the sync may start. Synchronous, so the usual boot (nothing new)
 * pays one localStorage read. Meaningless on a cold profile -- the full hydrate
 * covers it there.
 */
export function hasUnreconciledKeys(): boolean {
  return unreconciledKeys().length > 0
}

/**
 * Hydrate-before-flush for keys added to `DURABLE_PREF_KEYS` after this
 * profile last talked to the host (growth-gap issue 9491).
 *
 * A warm profile never runs the cold hydrate, so without this pass a newly
 * added key's local value -- typically a DEFAULT a hook persisted on mount --
 * would read as "changed" on the first flush and overwrite the value another
 * origin already backed up. The rules mirror `hydrateUiPrefs`, applied only to
 * the new keys:
 *
 *   * host has a value, local absent: adopt the host value (this origin never
 *     chose one). Counts as restored, so the caller reloads for the same
 *     module-scope-reader reason as the cold path.
 *   * host has a value, local present: keep local IN USE here but baseline it,
 *     so the first flush does not upload it; it goes up when the user changes
 *     it. Exception: after a FAILED reconcile, a key the profile did not hold
 *     at the failure was written by a settings-less render, so the HOST wins
 *     (the shared `HYDRATE_PENDING_KEY` contract -- a warm profile can only
 *     carry that marker from a failed reconcile, since a failed cold hydrate
 *     leaves the profile cold).
 *   * host has no value, local present: left out of the baseline on purpose,
 *     so the first flush uploads it and seeds the backup.
 *
 * Returns the number of keys written locally; rejects never (a failure returns
 * -1 so the caller can decline to start the sync, exactly like a failed cold
 * hydrate -- flushing without the reconcile is the clobber this exists to
 * prevent). Success writes the roster, so the pass costs one GET per
 * allowlist growth, not per boot.
 */
export async function reconcileNewDurableKeys(): Promise<number> {
  const fresh = unreconciledKeys()
  if (fresh.length === 0) {
    markReconciled()
    return 0
  }
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), HYDRATE_TIMEOUT_MS)
  const owned = ownedAtFailure()
  try {
    const res = await fetch(ENDPOINT, { signal: controller.signal })
    if (!res.ok) {
      markHydrateFailed()
      return -1
    }
    const body: unknown = await res.json()
    const prefs = (body as { prefs?: unknown } | null)?.prefs
    if (!prefs || typeof prefs !== 'object' || Array.isArray(prefs)) {
      // A 200 whose body is not a backup is NOT an empty backup. Recording the
      // roster here would start the sync and flush the unreconciled keys'
      // local defaults over whatever the host really holds -- fail instead,
      // and the next boot retries against a healthy gateway. Like every other
      // failure path, record the ownership snapshot: without it the retry
      // would read ownedAtFailure() as null and trust a default a hook mounts
      // AFTER this failure, keeping it over the host's real value forever.
      markHydrateFailed()
      return -1
    }
    // A legacy whole-blob host value supplies each child's value during
    // reconcile, exactly as it does on the hydrate path -- without this a warm
    // profile upgrading from a whole-blob build sees `undefined` for every
    // child, baselines nothing, and its first flush uploads this origin's stale
    // fields over a newer per-field backup (the #15236 clobber).
    const hostValues = withLegacyCompositeExpanded(prefs as Record<string, unknown>)
    // Reconcile the UNION of the local-derived fresh set and the composite
    // children the host holds -- but ONLY when the composite is itself being
    // reconciled this pass (`fresh` carries the parent trigger marker). A warm
    // profile reconciling an UNRELATED new plain key must not reach in and
    // adopt every host chat-config child: that is the cross-origin bulk import
    // design decision 1 and `PRE_ROSTER_KEYS` rule out, and it would inject
    // children into the agent-writable blob on a profile that never asked for
    // them (Opus 5, :725). The parent marker is dropped here (never a wire key);
    // a child present only on the host is added so it is adopted-and-baselined
    // too, without which a host-only field is never baselined and the next
    // whole-blob save uploads its default over the host's newer value (GPT F1).
    const reconcilingComposite = fresh.includes(COMPOSITE_KEY)
    const wireKeys = new Set<string>()
    for (const k of fresh) if (k !== COMPOSITE_KEY) wireKeys.add(k)
    const prints0 = readSyncedPrints()
    const known0 = readRoster() ?? new Set(PRE_ROSTER_KEYS)
    if (reconcilingComposite) {
      // Select host children by a MISSING FINGERPRINT alone -- NOT also "not in
      // the roster". An in-place downgrade to a pre-split build sheds the hex
      // child FINGERPRINTS (`readSyncedPrints` drops keys it does not know) but
      // carries the child ROSTER entries through verbatim and writes a fresh
      // parent fingerprint. On re-upgrade `legacyCompositeSynced()` makes the
      // pass run, but a `!known0.has(k)` term would exclude every such child
      // (its roster entry survived), leave `wireKeys` empty, baseline nothing,
      // and let the first flush upload all children over another origin's newer
      // per-field backup -- the #15236 clobber, via the exact
      // roster-without-fingerprint state the downgrade shed exists to prevent
      // (Opus 5, :790). A child missing its fingerprint has not been baselined
      // against the host on THIS build regardless of a stale roster entry, so
      // the fingerprint is the authoritative "already reconciled" signal here.
      for (const k of Object.keys(hostValues)) {
        if (childField(k) !== null && !prints0.has(k)) wireKeys.add(k)
      }
    }
    const updates = new Map<string, string>()
    // A child the user explicitly edited (recorded dirty at the saveChatConfig
    // seam) but that never uploaded -- the reconcile GET failed, so this
    // session never flushed (main.tsx boot(restored === 0)) -- must be kept
    // over the stale host value here, exactly as if it were owned. Without
    // this a failed reconcile followed by an edit of a blob-predating field
    // restores the host value AND baselines it, losing the edit silently on
    // both sides (GPT F1, uiPrefs.ts:913).
    const dirty = dirtyChildKeys()
    let restored = 0
    // Children whose REQUIRED host restore write was refused (quota). GPT 6.1
    // F2: such a child must NOT be marked reconciled — adding it to the roster
    // while its host value never landed makes the next flush read the stale
    // local value as authoritative and overwrite the (newer) host backup. We
    // both exclude it from `reconciledChildren` below AND fail the reconcile, so
    // the pending marker and migration state are kept for the next boot's retry.
    const failedChildRestores = new Set<string>()
    for (const key of wireKeys) {
      const value = hostValues[key]
      if (typeof value !== 'string') continue // host has nothing: local (if any) seeds it
      // A composite child's local value lives inside the blob, and a restore
      // writes it back INTO the blob rather than under the wire key.
      const field = childField(key)
      const local = field === null ? readPreference(key) : (expandComposite().get(key) ?? null)
      // A failed restore recorded per-field entries (plus the parent as a
      // downgrade signal); a child counts as owned by its EXACT entry, by the
      // parent only on a legacy child-less marker (see compositeChildOwned), or
      // by an un-uploaded local edit the dirty marker still records.
      const isOwned =
        dirty.has(key) ||
        (owned !== null && (field === null ? owned.has(key) : compositeChildOwned(owned, key)))
      const keepLocal = local !== null && (owned === null || isOwned)
      if (keepLocal) {
        updates.set(key, local)
        continue
      }
      if (local === value) {
        updates.set(key, local)
        continue
      }
      // A child whose host value is not valid JSON is skipped rather than
      // written as null over a valid local field.
      const parsed = field === null ? null : safeParse(value)
      if (field !== null && parsed === PARSE_FAILED) {
        if (local !== null) updates.set(key, local)
        continue
      }
      const wrote =
        field === null ? safeSetItem(key, value) : writeCompositeField(field, parsed)
      if (wrote) {
        updates.set(key, value)
        restored += 1
      } else if (field !== null) {
        // A composite child's required restore was refused by quota. Record it
        // so it is neither baselined nor marked reconciled, and the reconcile
        // fails below (GPT 6.1 F2).
        failedChildRestores.add(key)
      }
      // Dropped by quota: NOT baselined, or the first flush would read the
      // missing key as a deletion and null out a good host backup.
    }
    // Commit baselines and roster in ONE verified write. Two separate writes
    // opened a real clobber: the baseline write could be dropped by quota while
    // the roster write landed, and a roster without baselines starts the sync
    // whose first flush uploads the unreconciled keys' local values over the
    // host backup. One write cannot half-land, and a dropped write fails the
    // reconcile below, so the sync never starts on an uncommitted baseline.
    const prints = readSyncedPrints()
    for (const [k, v] of updates) prints.set(k, fingerprint(v))
    // The roster records every composite CHILD this pass reconciled, not just
    // the static `DURABLE_PREF_KEYS`. A child whose host value was absent gets
    // no fingerprint (there was nothing to baseline), so without a roster entry
    // it would read as unreconciled on every subsequent boot -- withheld from
    // flush forever while `commitSent` fingerprinted it from a never-sent value
    // (Opus 5, :634). A roster entry is the plain-key clearing mechanism applied
    // to children: "a reconcile already read the host for this and decided",
    // independent of whether anything was written. The children in `known0`
    // (already reconciled on a prior pass) carry forward.
    const reconciledChildren = new Set<string>(
      [...known0].filter((k) => childField(k) !== null),
    )
    for (const k of wireKeys) {
      // A child whose required host restore was refused by quota is NOT
      // reconciled: marking it so (while its host value never landed) would let
      // the next flush overwrite the newer host backup with the stale local
      // value (GPT 6.1 F2). It stays out of the roster and the reconcile fails
      // below, so the pending marker is retained and the next boot retries.
      if (childField(k) !== null && !failedChildRestores.has(k)) reconciledChildren.add(k)
    }
    // A required child restore was refused by quota: fail the reconcile so the
    // migration state (pending marker, legacy parent print, roster) is retained
    // for the next boot rather than committing a partial, uploadable baseline
    // (GPT 6.1 F2). Returning here BEFORE rewriting the synced doc keeps the
    // legacy parent print in place, so legacyCompositeSynced stays true and the
    // children remain withheld from flush.
    if (failedChildRestores.size > 0) {
      markHydrateFailed()
      return -1
    }
    const rosterList = [...DURABLE_PREF_KEYS, ...reconciledChildren]
    const doc: Record<string, unknown> = { [ROSTER_ENTRY]: JSON.stringify(rosterList) }
    for (const [k, v] of prints) doc[k] = v
    if (!safeSetItem(SYNCED_KEYS_KEY, JSON.stringify(doc))) {
      // The keys already restored above stay in localStorage: the next boot's
      // retry finds them present, keeps them, and baselines them then. Record
      // the ownership snapshot like every failure path (the restored keys hold
      // host values, so trusting them on retry is correct), or a default a
      // hook mounts after this point would be kept over the host's value.
      markHydrateFailed()
      return -1
    }
    try {
      localStorage.removeItem(HYDRATE_PENDING_KEY)
    } catch {
      /* best-effort */
    }
    if (restored > 0) window.dispatchEvent(new Event('mc-config-changed'))
    return restored
  } catch {
    markHydrateFailed()
    return -1
  } finally {
    clearTimeout(timer)
  }
}
/** Debounce for a change-triggered flush. Long enough that dragging a splitter
 *  produces one PUT, short enough that closing the window right after a click
 *  still catches it (the visibility flush is the backstop). */
const FLUSH_DEBOUNCE_MS = 1500
/** Safety-net scan for writers that fire no event we listen to. */
const POLL_INTERVAL_MS = 30_000
/** The boot-time hydrate must not hold the first paint hostage on a slow or
 *  unreachable gateway. On timeout we render with defaults, exactly as today. */
const HYDRATE_TIMEOUT_MS = 3000

/** Last state we successfully sent, so a flush PUTs only what changed. */
let lastSent: Map<string, string> | null = null
let flushTimer: ReturnType<typeof setTimeout> | undefined
let pollTimer: ReturnType<typeof setInterval> | undefined
let started = false
let inFlight: Promise<void> | null = null
/** A change arrived while a PUT was in flight: chain one more flush after it. */
let dirtyDuringFlush = false
/** Set while something else is rewriting the HOST copy (see `pauseUiPrefsSync`). */
let paused = false

function readLocalSnapshot(): Map<string, string> {
  const snapshot = new Map<string, string>()
  for (const key of DURABLE_PREF_KEYS) {
    if (key === COMPOSITE_KEY) {
      // The composite never travels as a whole blob: its fields each become
      // their own wire key, so the host merges them one at a time.
      for (const [child, value] of expandComposite()) snapshot.set(child, value)
      continue
    }
    const value = readPreference(key)
    if (value !== null) snapshot.set(key, value)
  }
  return snapshot
}

/**
 * Non-cryptographic change detector: two independent 32-bit FNV-1a lanes plus
 * the length, ~64 bits of separation. A collision here is not harmless — the
 * changed value is read as "already synced", never uploaded, and an origin
 * reset then restores the stale host copy over it — so one 32-bit lane
 * (2^-32 per change) was too thin. Two lanes with different offset bases and a
 * different multiplier walk unrelated orbits, and the length forecloses the
 * cheapest constructed collisions. Storing the prior values themselves would
 * be exact but doubles localStorage use for the largest keys; this is not a
 * digest and does not claim adversarial resistance.
 */
function fingerprint(value: string): string {
  let a = 0x811c9dc5
  let b = 0x9747b28c
  for (let i = 0; i < value.length; i++) {
    const c = value.charCodeAt(i)
    a ^= c
    a = Math.imul(a, 0x01000193)
    b ^= c
    b = Math.imul(b, 0x5bd1e995)
    b ^= b >>> 15
  }
  return `${value.length.toString(36)}.${(a >>> 0).toString(36)}.${(b >>> 0).toString(36)}`
}

function readSyncedPrints(): Map<string, string> {
  const raw = safeGetItem(SYNCED_KEYS_KEY)
  if (!raw) return new Map()
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return new Map()
    const out = new Map<string, string>()
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      // Ignore a fingerprint for a key THIS build does not know. On a downgrade
      // the marker still lists keys a newer build synced; keeping them would put
      // them in `previousKeys` while `current` can never hold them (they are not
      // read at all), so buildPatch would emit `null` and the older build would
      // delete a preference the newer one owns. A composite CHILD key is known
      // whenever its parent is a composite this build syncs, even though the
      // concrete field name is not in the static allowlist.
      if (typeof v === 'string' && isWireKey(k)) out.set(k, v)
    }
    return out
  } catch {
    return new Map()
  }
}

/**
 * Update the persisted fingerprints for SOME keys, leaving the rest alone.
 *
 * `updates` maps a key to its accepted value, or to `null` for a key the host
 * accepted a deletion of. Merging rather than replacing matters on the per-key
 * retry path: rebuilding the whole map from that round's few keys would erase the
 * fingerprints of every key the round never mentioned, and the next poll would
 * then re-upload their stale values over newer host preferences.
 */
function mergeSyncedPrints(updates: Map<string, string | null>): boolean {
  // Capture the two non-wire-key entries `readSyncedPrints` drops BEFORE the
  // rebuild, so the merge cannot silently delete them. The roster
  // (`currentRosterEntry`) and the legacy parent print (`readLegacyParentPrint`)
  // are both inputs other code reads out of this same doc; a rewrite that kept
  // only the wire-key fingerprints would erase the parent print and
  // self-destruct the unfinished-migration guard the moment a flush withheld
  // the children -- re-opening the #15236 clobber -- on EVERY caller, including
  // the per-key retry path (GPT 5.6 F1 / Opus 5, uiPrefs.ts:1070/:1320).
  // Preserving it here, the way the roster is already preserved, covers
  // `commitSent` and `retryPerKey` at one site and closes the two-write window a
  // separate replant would leave open.
  const prints = readSyncedPrints()
  const parentPrint = readLegacyParentPrint()
  for (const [k, v] of updates) {
    if (v === null) prints.delete(k)
    else prints.set(k, fingerprint(v))
  }
  const out: Record<string, unknown> = {}
  const roster = currentRosterEntry()
  if (roster !== null) out[ROSTER_ENTRY] = roster
  // The parent key is never a wire key, so an incoming update never carries it;
  // re-plant whatever was on disk. The migration completes when a later
  // reconcile clears the parent print at its own seam, not through this merge.
  if (parentPrint !== null) out[COMPOSITE_KEY] = parentPrint
  for (const [k, v] of prints) out[k] = v
  // Return whether the fingerprint doc actually persisted. A caller that is
  // about to clear a dirty marker on the strength of these fingerprints MUST
  // treat false as "the baseline did not advance": if storage is still full
  // after safeSetItem's free-and-retry, the merge is lost but the clear would
  // still run, leaving a field with no fingerprint AND no marker -- withheld on
  // every later flush, so a cold restore reinstates the stale host value with
  // no recovery path (GPT 6.1 F1, :1408). Keeping the marker lets the next
  // flush force the field up again.
  return safeSetItem(SYNCED_KEYS_KEY, JSON.stringify(out))
}

/**
 * The raw `COMPOSITE_KEY` fingerprint entry in the stored `SYNCED_KEYS_KEY`
 * document, or null when absent. This is the sole input to
 * `legacyCompositeSynced()`, and `readSyncedPrints` drops it (the parent is not
 * an `isWireKey`), so any `mergeSyncedPrints` rewrite silently deletes it. A
 * caller mid-migration must capture it before a merge and re-plant it after
 * (see `commitSent`), or a flush that withheld the children would self-destruct
 * the migration guard and re-open the #15236 clobber (Opus 5, :1212).
 */
function readLegacyParentPrint(): string | null {
  const raw = safeGetItem(SYNCED_KEYS_KEY)
  if (raw === null) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    const v = (parsed as Record<string, unknown>)[COMPOSITE_KEY]
    return typeof v === 'string' ? v : null
  } catch {
    return null
  }
}

/**
 * True when this profile has never successfully exchanged preferences with the
 * host — a fresh browser profile, a new origin (moved port), a relocated
 * Electron `userData`, or a boot whose fetch failed. That is exactly when the
 * backup should be read, and staying true after a failure is what makes the read
 * retry on the next boot instead of being forfeited.
 */
export function needsHydrate(): boolean {
  return safeGetItem(SYNCED_KEYS_KEY) === null
}

/**
 * Build the merge patch: changed and new keys as values, keys this profile
 * previously synced but no longer holds as `null`, so the backup follows a
 * deletion instead of resurrecting it.
 *
 * With no in-memory baseline (the first flush after a reload) the comparison runs
 * against the persisted fingerprints, so an unchanged value is NOT re-sent. That
 * matters beyond saving a request: re-sending everything from a profile holding
 * stale values would overwrite newer preferences another origin had already
 * backed up. A key this profile never synced is never nulled, which is what keeps
 * a second browser from deleting the first one's settings.
 */
function buildPatch(current: Map<string, string>): {
  patch: Record<string, string | null>
  withheld: Set<string>
} {
  const patch: Record<string, string | null> = {}
  const prints = lastSent ? null : readSyncedPrints()
  // Enforced at every flush, not only at boot: a key whose reconcile state is
  // uncommitted NEVER goes up. The boot-time reconcile alone left a producible
  // race -- in a mixed-version multi-tab session (a build upgrade with an old
  // tab still open), the OLD build's own baseline rewrite sheds the roster
  // entry mid-session, and this tab's next debounced flush would then read the
  // new keys as changed and upload their local defaults over the host backup.
  // Reading the unreconciled set on the flush path closes the whole class:
  // whoever drops the roster, the affected keys just stop flushing until a
  // boot reconciles them again. Steady-state cost is one localStorage read
  // per flush; the set is empty on every profile whose roster is intact.
  const withheld = new Set(unreconciledKeys())
  // A field the user EXPLICITLY edited (recorded at the saveChatConfig seam)
  // must upload even when it is unreconciled and even when it equals the known
  // default -- otherwise withholding drops a deliberate choice and a later
  // storage reset restores the stale host value over it (GPT 5.6 F1, :697).
  // The dirty marker is the only thing that can tell "the user chose the
  // default" from "a hook mounted the default", which the stored blob cannot.
  const dirty = dirtyChildKeys()
  for (const wireKey of dirty) withheld.delete(wireKey)
  for (const [key, value] of current) {
    if (withheld.has(key)) continue
    // A dirty child is always sent: its value may equal the baseline/default,
    // but the user chose it, so it must land on the host (and be baselined).
    const unchanged =
      !dirty.has(key) &&
      (lastSent ? lastSent.get(key) === value : prints!.get(key) === fingerprint(value))
    if (!unchanged) patch[key] = value
  }
  const previousKeys = lastSent ? new Set(lastSent.keys()) : new Set(prints!.keys())
  for (const key of previousKeys) {
    if (withheld.has(key)) continue
    if (!current.has(key)) patch[key] = null
  }
  return { patch, withheld }
}

async function putPatch(
  patch: Record<string, string | null>,
  keepalive = false,
): Promise<Response | null> {
  try {
    return await fetch(ENDPOINT, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prefs: patch }),
      // Set on the pagehide flush only: a plain fetch is cancelled with the
      // document, so a change made inside the debounce window before closing
      // was never sent -- and reopening on a NEW origin then restored the stale
      // host value over it (close, upgrade, port moves: this PR's own scenario).
      // Browsers cap keepalive bodies at 64 KiB; a larger patch rejects here,
      // returns null, and is retried on the next boot at the same origin.
      keepalive,
    })
  } catch {
    return null
  }
}

/**
 * Record a successful upload as the new baseline, on both sides -- but ONLY for
 * the keys that were actually sent. `buildPatch` withholds every unreconciled
 * key from the PUT, so baselining the whole snapshot would fingerprint a key
 * from a value never sent: that key then reads as "synced" forever, no boot
 * reconcile ever revisits it (`unreconciledKeys` sees a fingerprint), and the
 * user's chosen value is silently never backed up and is lost at the next cold
 * restore (GPT 5.6 / Opus 5, :693). A key that was never sent must not be
 * baselined.
 *
 * The next baseline is the prior one with exactly the sent keys applied: a key
 * present in `current` and not withheld takes its current value, a key the
 * patch deleted (previously baselined, not withheld, gone from `current`) is
 * dropped, and a WITHHELD key is left exactly as it was -- a transiently
 * withheld key keeps its real baseline, a never-synced withheld child keeps
 * having none and stays unreconciled until a boot reconcile reads the host.
 */
function commitSent(
  current: Map<string, string>,
  withheld: Set<string>,
  sentKeys: Set<string>,
): void {
  // In-memory baseline (value space).
  const prior = lastSent ?? new Map<string, string>()
  const next = new Map<string, string>(prior)
  for (const key of prior.keys()) {
    if (!withheld.has(key) && !current.has(key)) next.delete(key)
  }
  for (const [k, v] of current) if (!withheld.has(k)) next.set(k, v)
  lastSent = next
  // Persisted baseline (fingerprint space): the keys the host accepted get a
  // fresh fingerprint, the keys this round deleted are removed, and withheld
  // keys are untouched. Expressed as a merge so no stored fingerprint is ever
  // re-fingerprinted (mergeSyncedPrints fingerprints its string inputs, so a
  // withheld key left out of `updates` keeps its stored fingerprint as-is).
  const updates = new Map<string, string | null>()
  for (const [k, v] of current) if (!withheld.has(k)) updates.set(k, v)
  for (const k of readSyncedPrints().keys()) {
    if (!withheld.has(k) && !current.has(k)) updates.set(k, null)
  }
  // The legacy whole-blob parent fingerprint is the sole input to
  // `legacyCompositeSynced()`. It is not an `isWireKey`, so a naive rebuild of
  // the stored doc would drop it and self-destruct the migration guard the
  // moment any flush ran -- re-opening the #15236 clobber on every flush path,
  // the per-key retry included. `mergeSyncedPrints` now preserves it the way it
  // preserves the roster, so no caller has to capture-and-replant and the
  // two-write window a separate replant left open is closed. The migration
  // still COMPLETES: `reconcileNewDurableKeys` rewrites the doc directly without
  // the parent print once every child is reconciled, which is the one seam that
  // flips `legacyCompositeSynced()` false (GPT 5.6 F1 / Opus 5, :1070/:1320).
  const merged = mergeSyncedPrints(updates)
  // A dirty composite child that just landed now carries a real fingerprint,
  // so the marker that forced it up has done its one job -- drop it, else it
  // would force a redundant upload on every later flush. Clear ONLY children
  // that were in the ACKNOWLEDGED patch (`sentKeys`), never every non-withheld
  // key in `current`: `current` is the whole live snapshot, and a field the
  // user edited AFTER this flush's snapshot was taken (while this PUT was in
  // flight) is in `current` but was NEVER sent in this request. Clearing its
  // marker here would discard that unsent edit -- the next flush skips it and a
  // cold restore loses it (GPT 6.1 F1, :1297). Its marker survives because it is
  // not in `sentKeys`, so the next flush still forces it up.
  //
  // Gate the clear on the merge actually persisting: if `mergeSyncedPrints`
  // could not write the fingerprint doc (storage still full after its retry),
  // the baseline did NOT advance, so clearing the marker would strand the field
  // with neither a fingerprint nor a marker and lose the edit on cold restore
  // (GPT 6.1 F1, :1408). Keep the marker so the next flush forces it up again.
  if (!merged) return
  clearDirtyChildren(
    [...sentKeys].filter(
      (k) => !withheld.has(k) && current.has(k) && childField(k) !== null,
    ),
  )
}

/**
 * Re-send a refused patch one key at a time, so one unstorable value cannot
 * silently discard every other change in the same round. Keys the host accepts
 * enter the baseline; the offender stays out of it and is simply not backed up
 * (its local value still works, and the next flush tries it again).
 *
 * Only the keys THIS round touched are written to the baseline. Rebuilding it
 * from them would drop the fingerprints of every other synced key — after a
 * reload, where the in-memory baseline is empty, that is all of them — and the
 * next poll would re-upload their stale values over newer host preferences.
 */
async function retryPerKey(patch: Record<string, string | null>): Promise<void> {
  const hadBaseline = lastSent !== null
  const accepted = new Map<string, string>(lastSent ?? [])
  const landed = new Map<string, string | null>()
  for (const [key, value] of Object.entries(patch)) {
    const res = await putPatch({ [key]: value })
    if (res === null) break // went offline mid-retry: keep what landed, retry the rest
    if (!res.ok) continue
    landed.set(key, value)
    if (value === null) accepted.delete(key)
    else accepted.set(key, value)
  }
  if (landed.size === 0) return
  // Only adopt an in-memory baseline if there already WAS one. Built from an
  // empty start (the first flush after a reload) `accepted` holds just this
  // round's keys, and a PARTIAL in-memory baseline is worse than none: every key
  // missing from it reads as changed, so the next flush re-uploads stale values
  // over newer host preferences, and deletions of keys absent from it go
  // unreported. Left null, the next flush falls back to the persisted
  // fingerprints, which the merge below has just brought up to date.
  if (hadBaseline) lastSent = accepted
  const merged = mergeSyncedPrints(landed)
  // A dirty composite child that landed via per-key retry is now baselined --
  // clear its marker (see commitSent) so it does not force a later re-upload.
  // Gated on the merge persisting for the same reason as commitSent: a lost
  // fingerprint-doc write must not strand the field marker-less (GPT 6.1 F1).
  if (!merged) return
  clearDirtyChildren([...landed.keys()].filter((k) => childField(k) !== null))
}

/**
 * Apply the host backup onto `localStorage`, honouring local-wins and the
 * owned-at-failure contract at WIRE-key granularity, and return how many keys
 * were restored.
 *
 * A plain wire key restores the host string when this profile has no value for
 * it (or held an untrusted post-failure default). A composite child restores
 * one FIELD of its parent's blob under the same rules, and the parent's blob is
 * rewritten once with every restored field merged in -- so a field this origin
 * never set is filled from the host while a field it did set is kept, instead of
 * the whole blob being an all-or-nothing restore. `owned` is the set of wire
 * keys the profile held when a prior restore failed, or null when none did.
 */
function restoreHostValues(
  hostValues: Record<string, unknown>,
  owned: Set<string> | null,
): number {
  // hostValues is already legacy-expanded by the caller, so the baseline loop
  // in hydrateUiPrefs sees the same child keys this restore does.
  let restored = 0
  // Composite children are collected so the blob is written once.
  const localFields = expandComposite()
  // A child the user edited but never uploaded (dirty marker set, reconcile GET
  // failed) is kept over the stale host value here too -- hydrate must honour
  // the same un-uploaded-edit ownership the reconcile path does (GPT F1).
  const dirty = dirtyChildKeys()
  const winningFields = new Map<string, unknown>()
  for (const [wireKey, raw] of Object.entries(hostValues)) {
    if (typeof raw !== 'string' || !isWireKey(wireKey)) continue
    const field = childField(wireKey)
    if (field === null) {
      // Plain key: restore the host string only when local is absent or holds
      // an untrusted post-failure default.
      const local = readPreference(wireKey)
      if (local === raw) continue
      if (local !== null && (owned === null || owned.has(wireKey))) continue
      if (safeSetItem(wireKey, raw)) restored += 1
      continue
    }
    // Composite field: decide local-vs-host per field, then stage it.
    const localField = localFields.get(wireKey) ?? null
    if (localField === raw) continue
    // A failed restore recorded per-field entries (plus the parent as a
    // downgrade signal); a child counts as owned by its EXACT entry, by the
    // parent only on a legacy child-less marker (see compositeChildOwned), or
    // by an un-uploaded local edit the dirty marker still records (GPT F1).
    const isOwned = dirty.has(wireKey) || (owned !== null && compositeChildOwned(owned, wireKey))
    if (localField !== null && (owned === null || isOwned)) continue
    // A child whose host value is not valid JSON is skipped rather than staged
    // as null over a valid local field.
    const parsed = safeParse(raw)
    if (parsed === PARSE_FAILED) continue
    winningFields.set(field, parsed)
  }
  if (winningFields.size > 0) {
    const raw = readPreference(COMPOSITE_KEY)
    let blob: Record<string, unknown> = {}
    if (raw !== null) {
      try {
        const parsed: unknown = JSON.parse(raw)
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          blob = parsed as Record<string, unknown>
        }
      } catch {
        /* unparseable local blob: rebuild from the restored fields alone */
      }
    }
    for (const [field, value] of winningFields) blob[field] = value
    // Each field this write adds counts as one restore; a quota drop records
    // none, so the fields stay unbaselined and the next boot retries them.
    if (safeSetItem(COMPOSITE_KEY, JSON.stringify(blob))) {
      restored += winningFields.size
    }
  }
  return restored
}

/**
 * Read the host-side backup into `localStorage`.
 *
 * Only keys that are ABSENT locally are written, so this can never clobber a
 * value the running profile already has. Returns the number of keys restored.
 * Resolves (0) rather than rejecting when the gateway is unreachable: a missing
 * backup is not an error. A successful fetch — even an empty one — records the
 * synced key set, which is what stops the next boot from asking again.
 */
export async function hydrateUiPrefs(): Promise<number> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), HYDRATE_TIMEOUT_MS)
  // A previous restore on this profile failed; local values for keys the
  // profile did NOT hold at that moment are untrusted, so for those the host
  // wins (see the key's doc). Null means no failure on record: local wins.
  const owned = ownedAtFailure()
  try {
    const res = await fetch(ENDPOINT, { signal: controller.signal })
    if (!res.ok) {
      markHydrateFailed()
      return 0
    }
    const body: unknown = await res.json()
    const prefs = (body as { prefs?: unknown } | null)?.prefs
    if (!prefs || typeof prefs !== 'object') return 0
    const hostValues = prefs as Record<string, unknown>
    // Expand a legacy whole-blob backup into child keys ONCE, so the restore
    // below and the baseline loop that follows both see the same child wire
    // keys. Expanding only inside restoreHostValues left the baseline loop
    // blind to the children -- their fingerprints never landed, nothing was
    // withheld, and the first flush clobbered a newer backup.
    const expanded = withLegacyCompositeExpanded(hostValues)
    const restored = restoreHostValues(expanded, owned)
    // The host answered, so this profile is in touch with it. Baseline every
    // wire key the host holds with whatever is NOW local for it:
    //   * equal to the host's -- synced in the plain sense;
    //   * different from the host's (this profile kept its own value) -- also
    //     baselined, deliberately. This origin is by definition the one that has
    //     NOT been syncing; the host's copy came from an origin that has. Not
    //     baselining would make the first flush upload this profile's possibly
    //     months-stale value over the newer backup, unconditionally. Baselined,
    //     the local value stays in use here and is uploaded the moment the user
    //     changes it; the only way it is ever lost is a LATER storage wipe here,
    //     which then restores the host's -- a second failure, not the first.
    //   * a value `safeSetItem` had to drop (quota) stays OUT: recording it
    //     would make the first flush read it as a deletion and null out a good
    //     host backup. A composite child whose parent could not be written is
    //     therefore absent from the re-expanded snapshot and left unbaselined.
    const localWire = readLocalSnapshot()
    const syncedNow = new Map<string, string>()
    for (const wireKey of Object.keys(expanded)) {
      if (typeof expanded[wireKey] !== 'string' || !isWireKey(wireKey)) continue
      const local = localWire.get(wireKey)
      if (local !== undefined) syncedNow.set(wireKey, local)
    }
    // One write for baselines AND roster (the hydrate has, by definition,
    // reconciled every key this build knows, so the next boot must not pay the
    // growth-gap reconcile GET). Split writes could leave a roster without
    // baselines when quota drops the first write: the profile would then look
    // warm and reconciled, and the first flush would upload local values over
    // the backup. If this single write is dropped, the synced marker stays
    // absent and the next boot simply hydrates again -- today's behaviour.
    //
    // The roster records the composite CHILDREN this hydrate reconciled, not
    // only the static `DURABLE_PREF_KEYS` -- the host children it just read AND
    // the local blob's children. A child the local blob holds but the host does
    // NOT has no fingerprint (there was nothing to baseline), so without a
    // roster entry it would read as unreconciled on the next boot: withheld from
    // every flush and forcing a reconcile GET whose transient failure costs the
    // whole session's backup. The roster entry is the plain-key clearing
    // mechanism applied to a child the hydrate already decided on (Opus 5,
    // :1242).
    const hydratedChildren = new Set<string>()
    for (const k of Object.keys(expanded)) if (childField(k) !== null) hydratedChildren.add(k)
    for (const k of expandComposite().keys()) if (childField(k) !== null) hydratedChildren.add(k)
    const hydratedDoc: Record<string, unknown> = {
      [ROSTER_ENTRY]: JSON.stringify([...DURABLE_PREF_KEYS, ...hydratedChildren]),
    }
    for (const [k, v] of syncedNow) hydratedDoc[k] = fingerprint(v)
    safeSetItem(SYNCED_KEYS_KEY, JSON.stringify(hydratedDoc))
    // Cleared AFTER the synced marker is written, so a crash between the two
    // leaves the profile still pending rather than synced-with-untrusted-locals.
    try {
      localStorage.removeItem(HYDRATE_PENDING_KEY)
    } catch {
      /* best-effort */
    }
    if (restored > 0) {
      // Readers that already took their value need to re-read it. Some read it
      // at MODULE scope, before this function ran at all — static imports are
      // evaluated before any statement of the entry module, so e.g.
      // hooks/useBottomTerminal.ts captures its state into a module-level `let`
      // during import, and its own `storage` listener cannot help because
      // `storage` fires for OTHER documents, never for a write this document
      // made. Left unread, that stale module copy is persisted back over the
      // restored value by the first interaction.
      //
      // A same-document event cannot fix a value already captured, so the caller
      // reloads instead (see main.tsx). Announce the change as well for the
      // live listeners that CAN act on it, in case a caller chooses not to.
      window.dispatchEvent(new Event('mc-config-changed'))
    }
    return restored
  } catch {
    markHydrateFailed()
    return 0
  } finally {
    clearTimeout(timer)
  }
}

/** PUT whatever changed since the last successful flush. */
export async function flushUiPrefs(keepalive = false): Promise<void> {
  if (paused) return
  if (inFlight) {
    // Do NOT hand back the in-flight promise: it carries the OLD snapshot, so a
    // caller awaiting it (pagehide) would believe a newer change had been sent.
    // Mark dirty instead and let the running flush chain another round.
    dirtyDuringFlush = true
    return
  }
  const current = readLocalSnapshot()
  const { patch, withheld } = buildPatch(current)
  if (Object.keys(patch).length === 0) return

  inFlight = (async () => {
    try {
      const res = await putPatch(patch, keepalive)
      if (res === null) return // offline / gateway restarting — retry next trigger
      if (res.ok) {
        // The acknowledged request is `patch`; its keys are exactly what the
        // host accepted. Pass them so commitSent clears dirty markers only for
        // children actually in this PUT, never a field edited after the
        // snapshot while this request was in flight (GPT 6.1 F1).
        commitSent(current, withheld, new Set(Object.keys(patch)))
        return
      }
      if (res.status === 401 || res.status === 403) {
        // Either the access cookie lapsed (routine: useRefreshScheduler and the
        // API client's own recovery repair it within the session) or this is not
        // the owner's dashboard. Both look the same from here, and stopping for
        // the session on the first turned a routine cookie lapse into "nothing
        // changed after it is backed up until the next reload". So: treat like
        // 5xx -- baseline untouched, next trigger retries. A true non-owner
        // costs one refused PUT per change, at most every 30s.
        return
      }
      if (res.status >= 400 && res.status < 500) {
        // The server refuses a patch WHOLE, so nothing landed. Advancing the
        // baseline for every key would abandon the valid changes bundled with
        // the offending one.
        await retryPerKey(patch)
      }
      // 5xx: leave the baseline alone so the next trigger retries.
    } finally {
      inFlight = null
    }
  })()
  await inFlight
  if (dirtyDuringFlush) {
    dirtyDuringFlush = false
    await flushUiPrefs()
  }
}

function scheduleFlush(): void {
  if (flushTimer !== undefined) clearTimeout(flushTimer)
  flushTimer = setTimeout(() => {
    flushTimer = undefined
    void flushUiPrefs()
  }, FLUSH_DEBOUNCE_MS)
}

/** Unload-path flush: the request must outlive the document (see putPatch). */
function flushNow(): void {
  void flushUiPrefs(true)
}

function flushIfHidden(): void {
  if (document.visibilityState === 'hidden') flushNow()
}

/**
 * Start mirroring durable preferences to the host. Idempotent.
 *
 * The baseline starts empty on purpose, so the first flush uploads whatever
 * this profile currently holds. That is what seeds the backup for a user
 * upgrading into this feature, and on a just-hydrated cold profile it costs one
 * idempotent PUT of values the server already has — cheaper than a branch that
 * has to decide which case it is in.
 */
export function startUiPrefsSync(): void {
  if (started) return
  started = true
  lastSent = null

  window.addEventListener('mc-config-changed', scheduleFlush)
  // `storage` fires for OTHER tabs on the same origin, so a change made in one
  // window is backed up even while this one is idle.
  window.addEventListener('storage', scheduleFlush)
  // Best-effort catch of a change made just before the window goes away. A
  // `fetch` on unload may not complete; the debounce plus the poll are what
  // actually guarantee delivery, so nothing depends on this landing.
  window.addEventListener('pagehide', flushNow)
  document.addEventListener('visibilitychange', flushIfHidden)
  pollTimer = setInterval(scheduleFlush, POLL_INTERVAL_MS)
  // First flush right away: on a warm profile this is what creates the backup.
  scheduleFlush()
}

/** Resolve once no PUT is in flight, including one a finished round chained. */
async function settleInFlight(): Promise<void> {
  while (inFlight) {
    try {
      await inFlight
    } catch {
      /* the flush reports its own failures; this only waits for it to end */
    }
  }
}

/**
 * Upload what changed, then stop this page uploading preferences.
 *
 * For an operation that can rewrite the HOST copy -- the settings import:
 * Replace overwrites this host's `ui-prefs.json` with the archive's, and Merge
 * installs the archive's only where this host has none. A flush that
 * lands after it would put this page's values straight back over the ones just
 * restored, and the import has no way to tell. Paused before the import request
 * is sent, so no PUT can start after it either. Every flush path checks the
 * flag, the `pagehide` one included, so a reload that follows uploads nothing.
 * `resumeUiPrefsSync` undoes it when the host copy did not change after all.
 *
 * Flushed FIRST, not dropped: a change still inside the debounce window, or one
 * only the poll would have noticed, is otherwise never sent -- and the reload
 * that adopts the host copy then replaces it with the host's older value. Only
 * while the sync runs: a page that never started it (its hydrate failed) holds
 * untrusted locals, and uploading them is the clobber main.tsx refuses. A
 * failed flush does not stop the pause; the value stays local, as any failed
 * flush leaves it.
 */
export async function pauseUiPrefsSync(): Promise<void> {
  if (flushTimer !== undefined) {
    clearTimeout(flushTimer)
    flushTimer = undefined
  }
  if (started && !paused) {
    try {
      await settleInFlight()
      await flushUiPrefs()
      await settleInFlight()
    } catch {
      /* best-effort: the pause must hold whatever the upload did */
    }
  } else {
    await settleInFlight()
  }
  paused = true
  // A trigger that fired during the flush scheduled a timer; paused, it would
  // only no-op, so drop it.
  if (flushTimer !== undefined) {
    clearTimeout(flushTimer)
    flushTimer = undefined
  }
}

/** Undo `pauseUiPrefsSync`: the host copy was left alone, so syncing resumes. */
export function resumeUiPrefsSync(): void {
  if (!paused) return
  paused = false
  if (started) scheduleFlush()
}

/**
 * Make the NEXT page load take the host copy for every key the host holds,
 * after something rewrote that copy (a settings import restored it).
 *
 * The cold hydrate is the only reader of the backup, and it runs only while the
 * synced marker is absent -- so without this a restored `ui-prefs.json` is never
 * read on a profile that has synced before, and the next flush overwrites it
 * with this page's values. Removing the marker re-arms the hydrate; an EMPTY
 * hydrate-pending list is what makes it hand the host every key it holds (a key
 * not in that list is one the host wins, see `HYDRATE_PENDING_KEY`) instead of
 * keeping the local value as a never-failed hydrate would. A key the host does
 * not hold keeps its local value. The sync stays paused, so nothing uploads
 * between this and the reload the caller performs; the hydrate then reloads once
 * more for the module-scope readers, as it does for any restore.
 * Returns true only after the pending marker is written and the synced marker
 * is removed. On failure the synced marker stays in place, the pending marker
 * is rolled back, and the sync resumes; the caller must not reload.
 */
export async function adoptHostUiPrefsOnNextLoad(): Promise<boolean> {
  await pauseUiPrefsSync()
  if (!safeSetItem(HYDRATE_PENDING_KEY, JSON.stringify([]))) {
    resumeUiPrefsSync()
    return false
  }
  try {
    localStorage.removeItem(SYNCED_KEYS_KEY)
  } catch {
    try {
      localStorage.removeItem(HYDRATE_PENDING_KEY)
    } catch {
      /* storage blocked: rollback is best-effort, but the synced marker stays */
    }
    resumeUiPrefsSync()
    return false
  }
  // Explicit host adoption means "take the host copy for every field the host
  // holds". A pre-import chat edit that never flushed left a dirty marker, and
  // that marker makes `restoreHostValues` treat the child as locally-owned --
  // so the re-armed hydrate would KEEP the stale local edit over the value the
  // user explicitly imported, and the next flush would overwrite the host
  // (GPT 6.1 F1). Clear the chat dirty markers here, on the adoption path only:
  // an ordinary failed-restore retry does not go through this function, so its
  // markers (the un-uploaded-edit protection) are untouched. Best-effort -- a
  // failed clear does not void the adoption, it only risks keeping one local
  // edit, which is strictly better than the pre-fix state.
  try {
    localStorage.removeItem(COMPOSITE_DIRTY_KEY)
  } catch {
    /* storage blocked: adoption still proceeds; a stale marker is the old risk */
  }
  lastSent = null
  return true
}

export function __resetUiPrefsSyncForTests(): void {
  started = false
  paused = false
  if (flushTimer !== undefined) clearTimeout(flushTimer)
  if (pollTimer !== undefined) clearInterval(pollTimer)
  flushTimer = undefined
  pollTimer = undefined
  window.removeEventListener('mc-config-changed', scheduleFlush)
  window.removeEventListener('storage', scheduleFlush)
  window.removeEventListener('pagehide', flushNow)
  document.removeEventListener('visibilitychange', flushIfHidden)
  lastSent = null
  inFlight = null
  dirtyDuringFlush = false
}
