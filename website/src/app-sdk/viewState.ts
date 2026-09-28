/**
 * App view state — the small record that says WHERE the user was, owned by the host.
 *
 * A builtin page is unmounted on navigation (`BuiltinAppRoute` renders one lazy
 * component under a catch-all route, and React Router tears it down when you leave),
 * so component-local state returns to its defaults on every visit. This module is the
 * platform's answer: an app declares the few coordinates worth restoring, and the host
 * decides where they are written and what happens when they cannot be read back.
 *
 * Three properties are load-bearing, and each is a mechanism here rather than a
 * convention the caller is trusted to follow:
 *
 * 1. THE NAMESPACE IS THE HOST'S. The key is `kc:app:<appId>:view`, with appId taken
 *    from the identity context via `useTrustedAppId()`. An app never names its own
 *    namespace, and cannot: the id it would have to supply is not a parameter.
 *
 * 2. THE DECLARATION IS A FILTER, NOT DOCUMENTATION. `pickDeclared` runs on every
 *    write, so a field absent from `fields` cannot reach storage — passing a whole
 *    component state in persists only the declared subset. This is what makes "do not
 *    persist the drive's contents" enforced instead of merely written down, and it is
 *    the shape shared by Zed's `should_serialize` and Codex rollout's
 *    `is_persisted_rollout_item`.
 *
 * 3. THE FIRST READ IS SYNCHRONOUS. The record is read in a `useState` initializer, so
 *    the restored value exists on the consumer's FIRST render and anything keyed off it
 *    — a query key, most of all — is right the first time. Reading in an effect would
 *    look correct on a cold visit, because a `React.lazy` child is still suspended while
 *    its parent commits; on a repeat visit the module is already loaded, parent and child
 *    render in one pass, and an effect is a render too late. A repeat visit is the case
 *    this feature exists to serve, so the masking is not something to rely on. This is
 *    the same ordering `identity.ts` publishes from a render body for, and it is why the
 *    tests here assert the first render rather than the settled DOM.
 *
 * VERSIONING carries in the blob (`revision`), which is a new pattern in this frontend
 * and deliberately so. The two shapes already here are a version in the KEY
 * (`kc:file-explorer:state:v2`) and a tolerant partial parse that never versions at all
 * (issue-radar's `loadUiState`). Neither fits a store the host owns on behalf of many
 * apps: a version in the key makes the host's key FORMAT part of what every app has to
 * know, and pushes a migration into each app's key string, whereas one field inside the
 * record lets the store decide centrally and keeps the key a stable address.
 *
 * Imported by PATH and deliberately NOT re-exported from `./index`, following the
 * reasoning `useComposerDraft` records there and `identity.ts` repeats: the barrel is
 * what third-party apps resolve, held in exact agreement with
 * `public/vendor/kirocrew-app-sdk.mjs`, so a name added to it is published and freezing
 * a contract is easier than unfreezing one. There is nothing to publish yet — this store
 * is builtin-only BY CONSTRUCTION (see `useTrustedAppId`), so in a third-party app it
 * would be a hook that never persists anything.
 */
import { useCallback, useEffect, useState } from 'react'
import { useTrustedAppId } from './identity'
import { safeGetItem, safeRemoveItem, safeSetItem } from '../utils/safeStorage'

/**
 * What an app declares as its view state.
 *
 * Author this as a MODULE-LEVEL constant. The hook takes it as the identity of the
 * declaration, so an object rebuilt on every render costs a redundant write per render
 * (harmless — the content is identical — but pointless).
 */
export interface ViewStateDecl<T extends object> {
  /**
   * Which surface within the app this record belongs to — a key segment, not a label.
   *
   * Required, because the store's addressing unit has to match its CONSUMING unit. The
   * hook is used per component, each with its own declaration, so one record per app would
   * mean the second consumer's write emitting only ITS declared fields and erasing the
   * first's position. Worse than erasing it: the first consumer's read would find its
   * field merely ABSENT, fall back to the default, and report `restored` — a silent loss
   * dressed as a successful restore, invisible to either consumer's own tests.
   *
   * A segment in the KEY rather than several sections inside one record, deliberately. A
   * shared record has to be read-modify-written by every consumer, which is the shape that
   * lets one writer clobber a field it holds a stale copy of. Separate keys give each
   * surface an independent lifetime, so a corrupt record on one pane cannot take another
   * pane's position with it.
   */
  readonly name: string
  /**
   * Bump when a declared field's MEANING changes, which is the case a reader cannot
   * detect for itself: `path` going from a `/`-joined prefix to an array of segments
   * still type-checks as "present", so only an explicit revision separates the two.
   *
   * Adding a field does NOT require a bump — an older record simply lacks it and the
   * default fills in, which is the same tolerance issue-radar's partial parse relies on.
   *
   * Note what a revision canNOT express: a change to the KEY's shape. That is why `name`
   * is required from the start rather than introduced when a second consumer appears — by
   * then the old key holds real user records, and no in-blob field can migrate them.
   */
  readonly revision: number
  /**
   * The ONLY fields that may be persisted, one type guard each. The guard is used in
   * both directions: it filters what may be written, and it validates what is read
   * back, so a single declaration cannot drift out of agreement with itself.
   */
  readonly fields: { readonly [K in keyof T]: (value: unknown) => value is T[K] }
  /** The state to mount with when there is no usable record. */
  readonly defaults: T
}

/** Merge a patch into the live view state. Undeclared fields in the patch are dropped. */
export type ViewStateSetter<T extends object> = (patch: Partial<T>) => void

/**
 * Why a mount did not restore a record — the distinction the logging tiers rest on.
 *
 * `revision-mismatch` is a DESIGNED path, not a fault; `unreadable` is the only one that
 * says something went wrong.
 */
type ViewStateOutcome =
  /** No record on disk: a first visit, or a position that was reset to defaults. */
  | 'absent'
  /** A record was read and applied. */
  | 'restored'
  /** A record exists but was written under a different `revision`. */
  | 'revision-mismatch'
  /** Not JSON, not the record shape, or a declared field failed its own guard. */
  | 'unreadable'

interface ParsedViewState<T extends object> {
  /** Always a complete `T`: a restored record is merged over the defaults. */
  readonly state: T
  readonly outcome: ViewStateOutcome
}

/**
 * The on-disk record. `state` is untrusted until each field clears its guard.
 *
 * No `scope` field: the scope is in the KEY, so a record cannot be read under the wrong
 * one and there is nothing to compare.
 */
interface ViewRecord {
  revision: number
  state: Record<string, unknown>
}

/**
 * Charset for a key segment: non-empty, lowercase alphanumerics and `-`.
 *
 * Mirrors the appId rule in `apps/appIdentity.ts` for the same reason — `.`/`..` and the
 * separators are refused BY the charset, because a segment that can contain them can
 * address a namespace other than its own. Kept local rather than imported from `apps/`:
 * nothing in `app-sdk/` depends on that directory today, and a store should not acquire a
 * dependency on host registration machinery to spell one regex.
 */
const KEY_SEGMENT_RE = /^[a-z0-9-]+$/

/**
 * The host-owned key for one surface's view record, within one scope.
 *
 * `appId` has already cleared `isValidAppId` at registration, which is where an id becomes
 * a key segment. `name` has not been anywhere, so it is checked here.
 *
 * `scope` is DATA — an account id, a project — so it is encoded rather than validated. A
 * throw would be wrong for a value the user's environment supplies, and encoding is what
 * makes it safe: `encodeURIComponent` escapes `:`, so a scope cannot forge a segment
 * boundary and land in another scope's key.
 *
 * Scope lives in the key rather than inside the record for the same reason `name` does:
 * it makes the isolation structural instead of a comparison someone can forget. It also
 * means each scope keeps its OWN position, so a user working across two accounts finds
 * both folders where they left them — which a single record tagged with one scope could
 * not do, because whichever account was used last would overwrite the other.
 *
 * Growth is one small record per scope the user actually visits. That bounds it in
 * practice for a scope like an account, and it is the contract this key shape implies:
 * `scope` names a subject the user CHOOSES AMONG, not a per-item id. A high-cardinality
 * scope would accumulate records.
 *
 * Throws on a bad `name` rather than sanitizing, following `AppScopedApiProvider`'s
 * precedent in this same layer: `name` is authored in code as part of a module-level
 * declaration and is never data, so a bad one is a developer error that should fail
 * immediately and identically on every machine.
 */
function viewStateKey(appId: string, name: string, scope?: string): string {
  if (!KEY_SEGMENT_RE.test(name)) {
    throw new Error(
      `[app-sdk] view-state name ${JSON.stringify(name)} is not a valid key segment ` +
        `(lowercase letters, digits and '-' only). It names a storage key, not a label.`,
    )
  }
  const base = `kc:app:${appId}:view:${name}`
  // An unscoped consumer keeps the bare key rather than gaining an empty segment.
  return scope ? `${base}:${encodeURIComponent(scope)}` : base
}

/** A declared string field. The common case, and the only one AWS Control needs. */
export function isViewString(value: unknown): value is string {
  return typeof value === 'string'
}

/**
 * Declared field names in a stable order.
 *
 * Sorted rather than left in authoring order so the serialized record is byte-stable
 * for a given state regardless of how the declaration was written — which is what lets
 * `isDefaultState` compare two records by their serialization instead of walking them.
 */
function declaredFields<T extends object>(decl: ViewStateDecl<T>): (keyof T)[] {
  // `Object.keys` yields only string keys, so the string comparison below is total even
  // though `keyof T` admits symbols.
  const names = Object.keys(decl.fields).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))
  return names as (keyof T)[]
}

/**
 * Keep only the declared fields of `candidate` whose values clear their guard.
 *
 * This is the write-side gate, and it is the reason an app cannot persist wholesale:
 * anything not in `decl.fields` is not copied, so handing this a component's entire
 * state object yields only the coordinates. Deleting this call is what a test must
 * catch — see the drive-contents case in `viewState.test.ts`.
 */
function pickDeclared<T extends object>(decl: ViewStateDecl<T>, candidate: unknown): Partial<T> {
  const out: Partial<T> = {}
  if (candidate === null || typeof candidate !== 'object') return out
  const src = candidate as Record<string, unknown>
  for (const field of declaredFields(decl)) {
    const name = field as string
    if (!(name in src)) continue
    const value = src[name]
    if (decl.fields[field](value)) out[field] = value as T[keyof T]
  }
  return out
}

/**
 * The declared subset of `state`, merged over the defaults, serialized in a stable field
 * order.
 *
 * Merging matters: a state that simply does not mention a field IS that field's default,
 * so `{}` and `{path: ''}` have to canonicalize the same way or `isDefaultState` would
 * call an unset field a position worth storing.
 *
 * The restriction to declared fields is STRUCTURAL here, not a filter call: the loop
 * iterates `declaredFields(decl)` and copies nothing else, so an undeclared field cannot
 * reach the output whatever `state` contains. A `pickDeclared` call used to sit on the
 * merge as well, and it was removed as redundant -- every path in already carries filtered
 * values (`setView` filters a patch, `parseViewState` accepts only guard-passing fields),
 * so it could not change any output, which also means no test could observe it.
 */
function canonicalState<T extends object>(decl: ViewStateDecl<T>, state: Partial<T>): string {
  const merged = { ...decl.defaults, ...state } as T
  const ordered: Record<string, unknown> = {}
  for (const field of declaredFields(decl)) {
    ordered[field as string] = merged[field]
  }
  return JSON.stringify(ordered)
}

/**
 * Whether `state` carries nothing worth remembering.
 *
 * The store keeps a record only while there IS a position: returning to the defaults
 * removes the key rather than writing a record that says "nowhere in particular". That
 * keeps a visit to an app from leaving a row behind, and it makes discarding a stale
 * record the same operation as saving a default one — one path, not two.
 */
function isDefaultState<T extends object>(decl: ViewStateDecl<T>, state: Partial<T>): boolean {
  return canonicalState(decl, state) === canonicalState(decl, decl.defaults)
}

/** Serialize a record for storage. Only declared fields survive. */
function serializeViewRecord<T extends object>(
  decl: ViewStateDecl<T>,
  state: Partial<T>,
): string {
  return `{"revision":${JSON.stringify(decl.revision)},"state":${canonicalState(decl, state)}}`
}

/**
 * Read a stored record into a complete state, or fall back to the defaults.
 *
 * Every rejection lands on the same answer — mount with the defaults — because a
 * changed schema must never be able to stop a page from mounting. That is Zed's
 * behaviour with an unknown item kind: drop the tab, do not crash the workspace.
 *
 * A declared field that is PRESENT but fails its guard rejects the WHOLE record rather
 * than just that field. One rule ("a record either validates or it is discarded") is
 * predictable in a way that "some fields may survive" is not — a half-restored position
 * is a state the app was never written to handle, and the corrupt case is not one worth
 * salvaging.
 */
function parseViewState<T extends object>(
  raw: string | null,
  decl: ViewStateDecl<T>,
): ParsedViewState<T> {
  if (raw === null) return { state: decl.defaults, outcome: 'absent' }

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return { state: decl.defaults, outcome: 'unreadable' }
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { state: decl.defaults, outcome: 'unreadable' }
  }

  const record = parsed as Partial<ViewRecord>
  if (record.revision !== decl.revision) {
    return { state: decl.defaults, outcome: 'revision-mismatch' }
  }
  if (record.state === null || typeof record.state !== 'object' || Array.isArray(record.state)) {
    return { state: decl.defaults, outcome: 'unreadable' }
  }

  const src = record.state as Record<string, unknown>
  const accepted: Partial<T> = {}
  for (const field of declaredFields(decl)) {
    const name = field as string
    // Absent is fine — the default fills in, which is how a field added at the same
    // revision reads against an older record. Present-but-invalid is not.
    if (!(name in src)) continue
    const value = src[name]
    if (!decl.fields[field](value)) return { state: decl.defaults, outcome: 'unreadable' }
    accepted[field] = value as T[keyof T]
  }
  return { state: { ...decl.defaults, ...accepted }, outcome: 'restored' }
}

/** What the store should do to storage for a given live state. */
type ViewStateWrite =
  /** Store this exact serialized record. */
  | { readonly action: 'write'; readonly raw: string }
  /** Delete the key: this scope's position is back at its defaults. */
  | { readonly action: 'remove' }

/**
 * Decide what storage should hold for a given live state.
 *
 * Two cases, because the scope is in the key: a record can only ever be OUR scope's
 * record, so there is no question of whose position a delete would take. That is what
 * moving scope out of the record bought — this function previously had to read what was
 * already stored, parse it, and compare its scope tag before it could tell a reset of our
 * own position from the destruction of another account's.
 *
 * A default state removes the key rather than storing a record that says "nowhere in
 * particular", so a visit that goes nowhere leaves nothing behind, and discarding a stale
 * record is the same operation as saving a default one.
 */
function resolveViewStateWrite<T extends object>(
  decl: ViewStateDecl<T>,
  state: Partial<T>,
): ViewStateWrite {
  if (isDefaultState(decl, state)) return { action: 'remove' }
  return { action: 'write', raw: serializeViewRecord(decl, state) }
}

/** Keys already reported as unreadable, so one corrupt record warns once. */
const unreadableLogged = new Set<string>()

/**
 * Report why a mount did not restore, at a level matched to what the reader can do.
 *
 * A scope mismatch is ROUTINE — it fires every time the user switches account — so it
 * is silent; logging it would train everyone to ignore the channel. A revision mismatch
 * happens only after someone deliberately changed the schema, and one debug line there
 * costs nothing and answers "why did everyone's position reset" immediately. An
 * unreadable record is the only genuine fault: something wrote garbage under a
 * host-owned key, so it warns — once per key, following the ledger `useTrustedAppId`
 * already keeps for a refused namespace, because a refusal repeated every render drowns
 * out real signal.
 */
function reportViewOutcome(key: string, outcome: ViewStateOutcome): void {
  if (outcome === 'revision-mismatch') {
    // eslint-disable-next-line no-console -- a silently reset position is unexplainable otherwise
    console.debug(`[app-sdk] view state at ${key} was written under another revision; mounting with defaults`)
    return
  }
  if (outcome === 'unreadable') {
    if (unreadableLogged.has(key)) return
    unreadableLogged.add(key)
    // eslint-disable-next-line no-console -- a corrupt host-owned key is a real fault
    console.warn(`[app-sdk] view state at ${key} could not be read; mounting with defaults and discarding it`)
  }
}

/**
 * One mount's resolved view state, tagged with the key it was read for.
 *
 * The key alone is enough now that the scope is part of it: one comparison covers a
 * namespace granted late, an appId change, AND a scope change.
 */
interface ViewSnapshot<T extends object> {
  /** `null` when no namespace was granted. Tracked so a LATE namespace is picked up. */
  readonly key: string | null
  readonly state: T
  readonly outcome: ViewStateOutcome
}

function readSnapshot<T extends object>(
  key: string | null,
  decl: ViewStateDecl<T>,
): ViewSnapshot<T> {
  // No key means no namespace was granted (a host page, or a non-builtin app). Nothing
  // is read and nothing is written — the hook still returns usable defaults, so a
  // shared component does not have to know which case it is in.
  if (key === null) return { key, state: decl.defaults, outcome: 'absent' }
  const parsed = parseViewState(safeGetItem(key), decl)
  return { key, state: parsed.state, outcome: parsed.outcome }
}

/**
 * Restore and persist an app's declared view state.
 *
 * `opts.scope` is the thing the coordinates are coordinates WITHIN — the AWS account
 * for a drive path, and whatever plays that role for the next consumer. It is
 * first-class rather than left to each caller because a mismatched scope has to resolve
 * to the defaults through the SAME path as a mismatched revision: one implementation of
 * "mount with defaults" covering both, instead of the store handling one and every app
 * hand-rolling the other. A caller who forgot that comparison would restore a position
 * taken in one account into a different account's bucket, and the failure is silent.
 *
 * A scope CHANGE is handled here too, by re-reading during render (React's documented
 * "adjusting state when a prop changes" pattern) rather than in an effect. Without it,
 * a caller whose scope changes without a remount would carry the old position forward
 * and the next write would store it labelled with the NEW scope — the record would state
 * something false. Doing it in the hook makes that unreachable for every consumer
 * instead of depending on each one remembering to remount.
 */
export function useAppViewState<T extends object>(
  decl: ViewStateDecl<T>,
  opts?: { readonly scope?: string },
): readonly [T, ViewStateSetter<T>] {
  // The single builtin gate. Read through `useTrustedAppId()` and never by testing
  // `origin` here: one gate to audit, and its `null` already covers both a host page
  // and an external app.
  const appId = useTrustedAppId()
  const key = appId === null ? null : viewStateKey(appId, decl.name, opts?.scope)

  // Lazy initializer, so the stored record is in hand on the FIRST render — see the
  // module header for why an effect is too late on a repeat visit.
  const [snapshot, setSnapshot] = useState<ViewSnapshot<T>>(() => readSnapshot(key, decl))

  // Use the freshly-read snapshot for THIS render rather than waiting for the re-render
  // the setState schedules, so even the intermediate render is addressed correctly.
  //
  // ONE comparison covers three cases, because the key carries the appId, the surface and
  // the scope. A namespace can be granted LATE (AppHost forwards an installed app's origin
  // from data, so the value above a continuously-mounted page can change), the appId can
  // change, and the scope can change — and all three mean the same thing: the state in
  // hand belongs to a different address than the one being asked for now. Carrying it
  // forward would write one address's position into another's.
  let active = snapshot
  if (snapshot.key !== key) {
    active = readSnapshot(key, decl)
    setSnapshot(active)
  }

  const setView = useCallback<ViewStateSetter<T>>(
    (patch) => {
      // Filter on the way in as well as on the way out: an undeclared field never
      // enters the live state either, so what the app reads back and what is persisted
      // cannot disagree.
      setSnapshot((prev) => ({ ...prev, state: { ...prev.state, ...pickDeclared(decl, patch) } }))
    },
    [decl],
  )

  const { state, outcome } = active

  useEffect(() => {
    if (key === null) return
    reportViewOutcome(key, outcome)
  }, [key, outcome])

  useEffect(() => {
    if (key === null) return
    // Every rejection above resolved to the defaults, so discarding a stale record and
    // clearing a reset position are the same case here — one path, decided purely.
    const write = resolveViewStateWrite(decl, state)
    if (write.action === 'write') safeSetItem(key, write.raw)
    else safeRemoveItem(key)
  }, [key, decl, state])

  return [state, setView] as const
}
