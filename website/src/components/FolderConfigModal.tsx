import { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import { Zap, FolderOpen, ChevronRight, Check, Plus, X } from 'lucide-react'
import Modal from './Modal'
import ErrorNotice from './ErrorNotice'
import { Input, Btn } from './ui'
import FolderGlyph from './FolderGlyph'
import ProjectPicker from './ProjectPicker'
import SimpleSelect from './SimpleSelect'
import { FOLDER_COLOR_PALETTE } from './folderColorCatalog'
import { useImeGuard } from '../hooks/useImeGuard'
import { ApiError } from '../api/apiError'
import { parseErrorCode } from '../utils/errorReport'
import { resolveFolderAgent, resolveFolderProjectDir, resolveFolderSteeringDirs } from '../utils/folderAgent'
import { api } from '../api/client'
import { useQuery } from '@tanstack/react-query'
import { useDebouncedValue } from '../apps/file-explorer/hooks'
import { ChatFolder, ChatTag } from '../types'
import { i18nT } from '../i18n/t'

/** The folder fields this modal owns. */
export type FolderConfigField = 'name' | 'color' | 'icon' | 'projectDir' | 'defaultAgent' | 'tags' | 'steeringDirs'

export interface FolderConfigDraft {
  name: string
  /** Palette hex for the folder glyph tint; '' = default gray. */
  color: string
  /** Emoji icon replacing the default glyph; '' = default glyph. */
  icon: string
  /** True when the user asked for a fresh auto-generated icon ("reset to
   *  auto"). Mutually exclusive with a manual `icon` edit — the backend
   *  rejects the two in one request, so the modal never sends both: typing an
   *  emoji clears this flag, and pressing Auto-generate restores the seeded
   *  icon value. */
  regenerateIcon: boolean
  projectDir: string
  defaultAgent: string
  /** Tag ids the folder carries; copied onto new chats filed into it. */
  tags: string[]
  /** Extra steering directories loaded for every chat in this folder's subtree
   *  (accumulative with ancestors, resolved server-side from folder_id). */
  steeringDirs: string[]
  /** Fields the USER actually edited, measured against what the modal opened
   *  with. The caller must build its PATCH from this rather than diffing the
   *  draft against live cache: a field another client changed while the modal
   *  was open differs from the draft without the user having touched it, and
   *  re-sending the stale value silently reverts it. */
  touched: FolderConfigField[]
}

interface Props {
  open: boolean
  onClose: () => void
  /** 'create' collects a new folder; 'edit' amends `folder`. */
  mode: 'create' | 'edit'
  /** create: parent folder id ('' = top level). Ignored when mode='edit'. */
  parentId?: string
  /** edit: the folder being amended. Required when mode='edit'. */
  folder?: ChatFolder
  /** Every folder — powers the read-only destination breadcrumb. */
  folders: ChatFolder[]
  installedAgents: { name: string; scope?: string }[]
  /** Global default agent, shown as what an empty agent choice falls back to. */
  globalDefaultAgent?: string
  /** The tag vocabulary, powering the folder-tag picker. Empty/absent hides the
   *  picker entirely — a folder can only carry tags that already exist. */
  availableTags?: ChatTag[]
  /** True when the chat-tags query FAILED (vs still loading) — renders an
   *  error line instead of asserting an in-progress state indefinitely. */
  availableTagsFailed?: boolean
  /** Retries the failed tag-vocabulary query in place — rendered as an inline
   *  Retry action on the error line so recovery never requires dismissing the
   *  modal (closing would discard a mid-draft form). */
  onRetryTags: () => void
  /** Resolves on a persisted save; REJECTS on failure so the modal can stay
   *  open with the draft intact and surface the reason. */
  onSubmit: (draft: FolderConfigDraft) => Promise<void>
}

/** Ancestor chain for `id`, outermost first. Cycle-guarded like the sidebar's
 *  own folder walks — a corrupt parent_id must not spin. */
function ancestorChain(folders: ChatFolder[], id: string | undefined): ChatFolder[] {
  const out: ChatFolder[] = []
  const seen = new Set<string>()
  let cur = id ? folders.find(f => f.id === id) : undefined
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id)
    out.unshift(cur)
    cur = cur.parent_id ? folders.find(f => f.id === cur!.parent_id) : undefined
  }
  return out
}

const EMPTY: FolderConfigDraft = { name: '', color: '', icon: '', regenerateIcon: false, projectDir: '', defaultAgent: '', tags: [], steeringDirs: [], touched: [] }

/** Set-equality on two tag-id lists (order-insensitive): the picker toggles
 *  membership, so "changed?" is about which ids are present, not their order. */
function sameTags(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false
  const s = new Set(a)
  return b.every(id => s.has(id))
}

/** Order-SENSITIVE equality on two directory lists: steering dirs are an
 *  ordered list the user builds row by row (order can matter to a steering
 *  loader), so a reorder IS a change — unlike the order-insensitive tag set. */
function sameDirs(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((d, i) => d === b[i])
}

/**
 * One modal for both "New folder" and "Folder settings".
 *
 * Consolidates what used to be four surfaces: the inline name-only create input,
 * the ⋯-menu default-agent select, the ⋯-menu emoji grid, and the ⋯-menu
 * "Link project directory" ProjectPicker launch.
 *
 * The parent folder is deliberately NOT an input. Every entry point already
 * fixes it (root lane, column lane, or a specific folder's "New subfolder"), so
 * offering a picker would let the user contradict where they clicked. It is
 * restated as a read-only breadcrumb instead, because a centred modal loses the
 * spatial cue the inline input got for free from its own indentation.
 */
export default function FolderConfigModal({
  open, onClose, mode, parentId, folder, folders, installedAgents, globalDefaultAgent, availableTags, availableTagsFailed, onRetryTags, onSubmit,
}: Props) {
  const [draft, setDraft] = useState<FolderConfigDraft>(EMPTY)
  const [pickerOpen, setPickerOpen] = useState(false)
  // Which field the shared ProjectPicker is currently feeding: 'project'
  // REPLACES the scalar project_dir; 'steering' PUSHES the picked path into the
  // steering-dirs array. One picker instance serves both — routing by target
  // keeps folder-directory picking identical to every other project picker.
  const [pickerTarget, setPickerTarget] = useState<'project' | 'steering'>('project')
  // The backend rejects a free-typed project_dir (not absolute / not an existing
  // directory / sensitive path) with a 400. Submit used to be fire-and-forget,
  // so a rejection closed the modal and threw the whole draft away with no
  // feedback. Hold the modal open until the save actually lands.
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState('')
  // A rejected icon (`icon_invalid` / `regenerate_icon_invalid` from the
  // server) renders AT the Icon field, localized — not as the raw English
  // server message in the modal's top alert, which names no field. Only the
  // icon codes route here; every other failure keeps the top alert.
  const [iconErr, setIconErr] = useState(false)
  // What the draft looked like when the modal opened — the baseline for
  // "has the user actually typed something worth protecting?".
  const seedRef = useRef<FolderConfigDraft>(EMPTY)
  // F3: the inherited effective dir SNAPSHOTTED at seed time. `inheritedDir`
  // (below) is derived from the live `folders` prop, so an ancestor's project_dir
  // changing via a prop/server push while this modal is open would otherwise
  // shift the re-scope baseline underneath the user. `inheritedDirRef` mirrors
  // the live value each render; the seed effect copies it into
  // `seededInheritedDir` once, giving an immutable baseline for rescopeUnsettled.
  const inheritedDirRef = useRef<string | undefined>(undefined)
  const seededInheritedDir = useRef<string | undefined>(undefined)
  // The inherited agent is live prop-derived state too. Mirror it during render
  // and snapshot the EFFECTIVE agent at seed time, so an inheriting child's
  // empty own default_agent is not mistaken for an in-session agent change.
  const inheritedAgentRef = useRef<string | undefined>(undefined)
  const seededEffectiveAgent = useRef('')
  const browseRef = useRef<HTMLButtonElement>(null)
  const steeringBrowseRef = useRef<HTMLButtonElement>(null)
  const nameRef = useRef<HTMLInputElement>(null)
  // UX CONCERN 5633240977 (partial-path flag flicker): `rosterInFlight`
  // correctly suppresses the orphan flag during the debounce gap and while a
  // scan is pending, but it cannot see a PREFIX of the path the user is
  // typing toward settling into a real, scannable directory of its own — e.g.
  // pausing at `/Users/you/code` en route to `/Users/you/code/project`. That
  // prefix scan SETTLES (isFetching goes false, in-flight ends truthfully),
  // so a settled prefix roster is indistinguishable from a settled
  // destination roster, and the orphan flag/notice fire against a directory
  // the user was only passing through.
  //
  // This is a different axis from in-flight-ness: "is the fetch settled" vs.
  // "has the user finished telling us which directory they mean". True from
  // focus, false on blur / Enter-commit / a ProjectPicker pick — the three
  // ways this field's edit ends. Only the FLAG (and everything the flag
  // gates: the notice, and Save via `blockingOrphan`) defers here — the scan
  // itself, `rosterInFlight`, `rosterScanError`, and the loading hint are
  // unchanged, so the debounce, the re-scope recovery states, and the
  // Save-gating machinery around them stay exactly as reviewed.
  const [projectDirFieldActive, setProjectDirFieldActive] = useState(false)
  // A folder name is prime IME territory (the sidebar's inline input it replaces
  // guarded this too). Without the guard, the Enter that COMMITS a Chinese /
  // Japanese / Korean composition also submits the form — creating a folder
  // named after a half-typed word.
  const ime = useImeGuard()

  // Re-seed whenever the modal opens (or retargets to a DIFFERENT folder) so a
  // previous session's draft never leaks into the next open.
  //
  // Keyed on the folder's ID, never the object identity: `folder` is a fresh
  // object every time the chat-folders cache changes, and a rejected edit
  // produces three such changes in a row (optimistic write -> rollback ->
  // invalidate). Depending on identity re-ran this effect mid-failure and
  // re-seeded from the persisted folder, erasing the very draft the
  // keep-open-on-error fix exists to preserve.
  const folderRef = useRef(folder)
  folderRef.current = folder
  // Read through a ref for the same reason as `folderRef`: the vocabulary must
  // not be an effect dependency, or a tag edit elsewhere would re-seed and
  // erase an open draft.
  const availableTagsRef = useRef(availableTags)
  availableTagsRef.current = availableTags
  // Read through a ref for the same reason: the global roster feeds the
  // dir-cleared reconciliation inside the fetch effect, but must NOT be an
  // effect dependency — `installedAgents` is a fresh array on many parent
  // renders, and depending on it would re-run the debounced scan spuriously.
  const installedAgentsRef = useRef(installedAgents)
  installedAgentsRef.current = installedAgents
  // Re-seed the draft whenever the modal opens or retargets to a different
  // folder. Keyed on the folder id (see the seed effect below).
  const seedKey = mode === 'edit' ? folder?.id : ''
  useEffect(() => {
    if (!open) return
    const f = folderRef.current
    // Seed only ids that exist in the current vocabulary — but ONLY when the
    // vocabulary is actually known. `availableTags` is undefined while the
    // tags query is unresolved; filtering against that as if it were an empty
    // vocabulary would seed a partial list on a cold load, and the next save
    // would silently delete the folder's existing tags. Unknown vocabulary
    // keeps the raw ids; the submit-time prune below (which runs once the
    // vocabulary has resolved) still clears genuinely dangling ids, so a save
    // never 400s over a reference the picker cannot display.
    const known = Array.isArray(availableTagsRef.current)
    const vocab = new Set((availableTagsRef.current ?? []).map(t => t.id))
    const seeded: FolderConfigDraft = mode === 'edit' && f
      ? {
        name: f.name ?? '',
        color: f.color ?? '',
        icon: f.icon ?? '',
        regenerateIcon: false,
        projectDir: f.project_dir ?? '',
        defaultAgent: f.default_agent ?? '',
        tags: Array.isArray(f.tags) ? (known ? f.tags.filter(t => vocab.has(t)) : [...f.tags]) : [],
        steeringDirs: Array.isArray(f.steering_dirs) ? [...f.steering_dirs] : [],
        touched: [],
      }
      : EMPTY
    setDraft(seeded)
    seedRef.current = seeded
    // F3: freeze the inherited effective dir as it is at open, so a later
    // ancestor prop change cannot move the re-scope baseline.
    seededInheritedDir.current = inheritedDirRef.current
    seededEffectiveAgent.current = seeded.defaultAgent || inheritedAgentRef.current || ''
    setPickerOpen(false)
    setPickerTarget('project')
    setSaving(false); setSaveErr(''); setIconErr(false)
    setProjectDirFieldActive(false)
  }, [open, mode, seedKey])

  // Focus the name field on open. rAF + preventScroll for the same reason the
  // sidebar's inline inputs need it: these open from a Radix menu, whose teardown
  // otherwise wins the focus race and yanks the scroll container sideways.
  useEffect(() => {
    if (!open) return
    const raf = requestAnimationFrame(() => nameRef.current?.focus({ preventScroll: true }))
    return () => cancelAnimationFrame(raf)
  }, [open])

  // Destination: for create, the parent chain plus a "new folder" leaf. For edit,
  // the folder's own path with itself as the leaf.
  const chain = useMemo(
    () => ancestorChain(folders, mode === 'edit' ? folder?.parent_id : parentId),
    [folders, mode, folder?.parent_id, parentId]
  )

  // An empty project directory means "inherit", and inheritance is real:
  // resolveFolderProjectDir walks up ancestors. So show what WOULD be inherited
  // as placeholder text rather than pre-filling it — pre-filling would write a
  // duplicate explicit value and silently break the link to the ancestor.
  const inheritedDir = useMemo(() => {
    const from = mode === 'edit' ? folder?.parent_id : parentId
    return from ? resolveFolderProjectDir(folders, from) : undefined
  }, [folders, mode, folder?.parent_id, parentId])
  // Mirror the live value so the seed effect (which does not depend on
  // `inheritedDir`) can snapshot it at open for the F3 immutable baseline.
  inheritedDirRef.current = inheritedDir

  // The project directory whose agents this folder's chats would actually run
  // under: the draft's own value when set, else the inherited ancestor value.
  // This is what the agent roster must be scoped to — NOT the active chat
  // slot's project, which is what the `installedAgents` prop carries and is
  // unrelated to the folder being configured.
  const effectiveProjectDir = draft.projectDir.trim() || inheritedDir || ''

  // Agents discovered under `effectiveProjectDir` (its `.kiro/agents/*.json`),
  // fetched from the backend's `?project_path=` scope via react-query. `null`
  // (no dir set) means "use the global `installedAgents` prop"; a non-null array
  // is that dir's roster (global rows unioned with the dir's project rows, per
  // /api/agents).
  //
  // The folder modal has no chat slot, so `useAgents` — which resolves project
  // scope from the slot — cannot serve it; this is a slot-independent lookup
  // keyed on a draft directory the user is still typing. It is a directory-keyed
  // `useQuery` (project convention: server state lives in react-query, not a
  // hand-rolled effect) with the directory DEBOUNCED, so a keystroke does not
  // fire a filesystem scan on the backend until typing settles. A ProjectPicker
  // selection sets the whole path at once and lands one debounce later too.
  const debouncedProjectDir = useDebouncedValue(effectiveProjectDir, 300)
  const projectRosterQuery = useQuery({
    // sessionKey omitted deliberately: this roster is for the DRAFT directory,
    // not any slot. The backend reads `project_path` from the query string.
    queryKey: ['folder-project-agents', debouncedProjectDir],
    queryFn: () => api.kirocrewAgents(undefined, debouncedProjectDir),
    // Only scan when a directory is actually set AND the modal is open.
    enabled: open && !!debouncedProjectDir,
    // staleTime 0: a re-scope back to a previously-seen dir must REFETCH, not
    // serve a cached roster instantly — otherwise a fresh cache entry lets a
    // pick validate against a stale roster without isFetching ever going true,
    // so Save could persist an agent the dir no longer has. Refetching keeps
    // isFetching true (and thus Save blocked) until the current scope is
    // re-validated. The debounce already bounds how often this fires.
    staleTime: 0,
    // retry:false so a failed scan is TERMINAL on the first attempt — the whole
    // error path (rosterScanError, the ErrorNotice + Retry row, the Save gating
    // comments that say "retry:false, so isError never clears on its own")
    // assumes isError reflects a settled failure. Without this, react-query's
    // default 3 silent retries delay isError going true, deferring the terminal
    // error notice and re-hitting the backend directory scan three times. The
    // explicit Retry button is the only re-scan path.
    retry: false,
  })
  // `null` when no dir is set OR the scan errored — both fall back to the global
  // prop roster, so the picker never blanks to a wrong empty selection. Only a
  // SUCCESSFUL scan replaces the roster with the dir's agents.
  const projectRoster: { name: string }[] | null =
    debouncedProjectDir && projectRosterQuery.isSuccess
      ? projectRosterQuery.data.agents || []
      : null

  // The default agent inherits the same way, so the empty option has to name the
  // agent an empty selection would ACTUALLY run: the nearest ancestor that pins
  // one, and only then the global default. Naming the global default
  // unconditionally reads "Inherit (kirocrew)" on a subfolder of an
  // agent-pinned folder whose chats will in fact run that ancestor's agent.
  const inheritedAgent = useMemo(() => {
    const from = mode === 'edit' ? folder?.parent_id : parentId
    return from
      ? resolveFolderAgent(folders, from, globalDefaultAgent || '')
      : globalDefaultAgent || undefined
  }, [folders, mode, folder?.parent_id, parentId, globalDefaultAgent])
  // Mirror the live value so the seed effect can freeze the effective agent at
  // open without depending on ancestor prop changes and re-seeding the draft.
  inheritedAgentRef.current = inheritedAgent
  // This is the agent chat creation would actually use: an explicit draft pick
  // wins, otherwise the nearest ancestor's agent (then the global default).
  // Every scope guard below must validate this effective value — an empty own
  // pick on a child still runs its inherited agent.
  const effectiveAgent = draft.defaultAgent || inheritedAgent || ''

  // Steering dirs accumulate up the chain, so the ANCESTORS' dirs are in effect
  // for this folder in addition to its own — shown read-only below the editable
  // list so the user sees the full effective set. Resolved from the parent chain
  // only (this folder's own dirs are the editable rows), and filtered by this
  // folder's principal the way the backend delivery gate filters: an ancestor
  // owned by ANOTHER principal never reaches this folder's chats, so it is not
  // listed as inherited. A folder being created from this dashboard is the
  // person's (no owner_app), so its principal is the empty string.
  const inheritedSteeringDirs = useMemo(() => {
    const from = mode === 'edit' ? folder?.parent_id : parentId
    const principal = (mode === 'edit' ? folder?.owner_app : '') || ''
    return from ? resolveFolderSteeringDirs(folders, from, principal) : []
  }, [folders, mode, folder?.parent_id, folder?.owner_app, parentId])

  const trimmedName = draft.name.trim()

  // The roster the picker actually offers: with an effective dir set, that
  // directory's project agents UNIONED with the globally installed templates;
  // otherwise ONLY the GLOBAL rows of the `installedAgents` prop. Scoping the
  // dropdown to the folder's own directory is the whole fix — the prop is the
  // ACTIVE SLOT's roster, which has nothing to do with the folder being
  // configured. Its `scope:'project'` rows belong to the active chat's project,
  // NOT this folder, so with no effective dir they must be excluded: offering
  // one lets it be saved as the folder default, and every chat opened under a
  // dir-less folder then fails "unavailable" at dispatch (a foreign
  // project-scoped agent does not resolve). Only global agents are valid for a
  // folder that pins no directory of its own.
  const globalInstalledAgents = installedAgents.filter(a => a.scope !== 'project')
  // UNION, not replace. `projectRoster` comes from GET /api/agents?project_path=,
  // whose rows are only `cfg.agents.items()` plus that dir's project names — it
  // does NOT carry the installed TEMPLATES (those come only from the agent
  // catalog). With `config.agents` empty by default, replacing the roster with
  // `projectRoster` would strip every installed template (including `kirocrew`)
  // the instant a directory is set, and an edit folder pinned to one of them
  // would be flagged "(not in this project)" and blocked from Save. So append the
  // global templates the project roster lacks, de-duplicated by name (an agent in
  // both sources appears once). An agent in NEITHER source is still absent, so it
  // keeps its genuine "(not in this project)" flag and still blocks Save.
  const effectiveAgents: { name: string }[] = projectRoster
    ? [
        ...projectRoster,
        ...globalInstalledAgents.filter(
          g => !projectRoster.some(p => p.name === g.name),
        ),
      ]
    : globalInstalledAgents

  // A folder can reference an agent that is not present in the effective roster —
  // an edit-mode folder whose saved default_agent was since uninstalled/renamed,
  // or a directory that does not expose it. Keep it SELECTABLE and flagged so the
  // user sees why, and BLOCK Save only on a SESSION-CHANGED orphan pick (see
  // blockingOrphan / canSubmit): a fresh invalid choice is a validation error the
  // user must resolve, but an edit folder's OWN saved orphan round-trips a benign
  // rename/recolor (base #1182) — run time fails loud if it is genuinely gone.
  //
  const seededEffectiveDir =
    seedRef.current.projectDir.trim() || seededInheritedDir.current || ''
  // Roster IN FLIGHT for the current effective dir — the debounce gap (typed dir
  // has not settled into the debounced value) OR the scan for the settled dir is
  // pending. This is a TRANSIENT state that resolves on its own. It does NOT
  // include `isError`: a terminal scan error is settled, not in flight.
  const rosterInFlight =
    effectiveProjectDir !== debouncedProjectDir
    || (!!debouncedProjectDir && projectRosterQuery.isFetching)
  // Roster UNSETTLED for the orphan FLAG — in flight OR the scan errored. On an
  // error the roster is unknown (effectiveAgents falls back to the global prop),
  // so a valid project agent would look absent; suppressing the flag on error
  // avoids a false "(not installed)" and a global-roster fallback.
  const rosterUnsettled =
    rosterInFlight || (!!debouncedProjectDir && projectRosterQuery.isError)
  // The scan for the current dir FAILED and is terminal (retry:false). The
  // roster is unknown, so an AGENT selection cannot be validated against the
  // dir's real scope — but a name-only folder (no agent) has nothing to
  // validate and still saves. Surfaced via ErrorNotice with a Retry.
  const rosterScanError = !!debouncedProjectDir && projectRosterQuery.isError
  // The error row is the ONLY element that names why Save went dead, and it sits
  // at the bottom of a scrollable modal body — reviewers found it half-clipped at
  // the fold on every failed scan, so the explanation was below the fold exactly
  // when it was needed. Bring it into view as the error appears rather than making
  // the user go looking for it. `block: 'nearest'` scrolls the minimum distance,
  // so a row already visible does not jump. The optional call keeps this harmless
  // under jsdom, which does not implement scrollIntoView.
  const rosterErrorRef = useRef<HTMLSpanElement | null>(null)
  useEffect(() => {
    if (!rosterScanError) return
    rosterErrorRef.current?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' })
  }, [rosterScanError])
  // Suppress the orphan FLAG while the roster is unsettled — mid-load OR on a
  // failed scan — so a valid project agent is not mislabelled as absent. Once
  // the roster settles SUCCESSFULLY, the effective agent (explicit or inherited)
  // is flagged when the roster lacks it.
  //
  // `orphanAgentGate` is the TRUTH the Save button is gated on — it does NOT
  // defer for `projectDirFieldActive`. Only what the user SEES (the trigger
  // wording and the warn notice, both driven by `orphanAgent` below) defers
  // while the directory field is active; the underlying invalid-combination
  // fact does not become less true just because the alarm about it is
  // deferred. Regression (UX Review, "flicker fix opened an Enter bypass"): an
  // earlier version deferred this SAME computation, which fed `blockingOrphan`
  // -> `canSubmit`, so Save silently unblocked for the whole span the
  // directory field was focused — including at the Enter keystroke that
  // commits the field and calls `submit()` in the same tick, before the
  // deferred alarm ever has a chance to re-arm. Save must stay dead through
  // that keystroke; only the ALARM text is allowed to lag.
  const orphanAgentGate = effectiveAgent && !rosterUnsettled
    && !effectiveAgents.some(a => a.name === effectiveAgent)
    ? effectiveAgent
    : ''
  // `orphanAgent` is the DISPLAY value — everything the user reads (the select
  // trigger's flagged label, the warn notice, its `aria-describedby` link).
  // Deferring it while `projectDirFieldActive` is true is UX Review's actual
  // ask (comment 5633240977): a SETTLED prefix roster (e.g. pausing at
  // `/Users/you/code` en route to `/Users/you/code/project`) is a real
  // settlement, just of the wrong directory, and `rosterUnsettled` alone
  // cannot see that — so without this the flag/notice would flash against a
  // directory the user was only passing through. This must NOT feed
  // `blockingOrphan`; only `orphanAgentGate` may.
  const orphanAgent = orphanAgentGate && !projectDirFieldActive ? orphanAgentGate : ''

  // The one round-trip exception is an edit folder's OWN saved explicit orphan
  // under its SEEDED directory. It stays savable for a benign rename/recolor
  // rather than forcing the user to discard a recoverable binding (base #1182).
  // Every other absent effective agent is a new invalid combination: either the
  // explicit pick changed, the directory changed, or an empty own pick resolves
  // to an inherited agent that the effective directory does not expose.
  //
  // Gated on `orphanAgentGate`, NOT the deferred `orphanAgent` — Save must stay
  // blocked for the currently-typed directory's settled roster regardless of
  // whether the user-visible alarm is currently deferred (see `orphanAgentGate`
  // above). `seedRef.current.defaultAgent` / `seededEffectiveDir` are unaffected
  // by field-active deferral, so the round-trip exception is unchanged.
  const blockingOrphan = !!orphanAgentGate
    && (orphanAgentGate !== seedRef.current.defaultAgent
      || effectiveProjectDir !== seededEffectiveDir)

  // Save is blocked on a name and on an invalid effective agent absent from the
  // settled roster. Directory validity is enforced by the backend (a
  // non-existent / non-absolute / sensitive path 400s and surfaces as saveErr).
  // Should a saved OWN agent later disappear, the narrow round-trip exception
  // above preserves benign edits and chat start still fails loud rather than
  // silently substituting another agent.
  //
  // `!rescopeUnsettled` closes the stale-roster window: after the effective dir
  // or agent changes, the roster does not yet describe the combination being
  // saved (debounce gap, then scan), and the orphan flag is suppressed meanwhile.
  // The initial open of an edit folder's unchanged own explicit selection stays
  // outside this gate; an inherited effective agent is validated because an
  // empty own field does not mean chat creation runs no agent.
  //
  // Keyed on rosterInFlight, NOT rosterUnsettled: a terminal scan error is a
  // distinct settled state handled by unvalidatableAgent below. Folding isError
  // into this transient guard would erase the global-agent round-trip exception.
  //
  // Two independent in-flight triggers: (a) the effective dir differs from the
  // seeded one (a genuine re-scope), and (b) the AGENT differs from its seeded
  // value while a scan is in flight — even back AT the seeded dir. Without (b),
  // the return-to-seed path escapes: pick a foreign agent under dir B, change
  // back to seeded dir A, and effectiveProjectDir === seededEffectiveDir, so the
  // debounced roster still shows B (where the pick looks valid) and Save would
  // enable mid-refetch, persisting an agent A's scope lacks. Gating on the
  // session-changed pick closes that window regardless of which dir is showing.
  //
  // Gated FIRST on the agent that chat creation would actually use. A child
  // with an empty own pick can still inherit an agent from its parent, so that
  // effective agent must remain guarded through the debounce+scan window. Only
  // an empty EFFECTIVE agent has nothing agent-vs-scope to validate; keeping
  // that case outside this gate preserves the no-agent Save behaviour from UX
  // finding 9884d6aad247. The isError path stays separate below.
  const rescopeUnsettled =
    !!effectiveAgent
    && rosterInFlight
    && (effectiveProjectDir !== seededEffectiveDir
      || effectiveAgent !== seededEffectiveAgent.current)
  // Blocked when the effective agent (explicit OR inherited) is absent from
  // the fallback roster after the current directory scan errors. This remains
  // keyed on isError, separate from rosterInFlight: a failed scan cannot prove
  // that a project-scoped effective agent is valid. A globally visible agent
  // still round-trips because it remains present in effectiveAgents, and an
  // empty effective agent remains unaffected.
  const unvalidatableAgent =
    rosterScanError
    && !!effectiveAgent
    && !effectiveAgents.some(a => a.name === effectiveAgent)
  const canSubmit =
    trimmedName.length > 0 && !blockingOrphan && !rescopeUnsettled && !unvalidatableAgent

  // Values and display labels as two PARALLEL arrays, orphan first so it keeps
  // the position its <option> held. The '' ("None" / "Inherit (x)") row is
  // SimpleSelect's `clearLabel` rather than a member of these arrays.
  //
  // Label wording depends on WHY the agent is absent (UX Review): "(not in this
  // project)" names the actual problem when the agent may well be installed and
  // valid elsewhere, rather than "(not installed)", which asserts a false fact
  // about the agent's global existence. That is true whenever EITHER (a) the
  // effective dir differs from the seeded one (a re-scope away from where the
  // folder started), OR (b) the orphan is a SESSION-CHANGED pick (differs from
  // seedRef.current.defaultAgent) — a pick the user just made under some
  // directory is presumptively valid there, so losing that directory (including
  // a create folder's dir being cleared back to its empty seed) is a scope
  // problem, not an uninstall. Only an orphan that is BOTH seed-equal AND at the
  // seeded dir (a genuinely uninstalled/removed saved agent, seen on open) keeps
  // "(not installed)".
  const orphanIsRescopeOnly =
    !!orphanAgent
    && (effectiveProjectDir !== seededEffectiveDir
      || orphanAgent !== seedRef.current.defaultAgent)
  // The DIR-LESS variant of the rescope case: an orphan flagged while NO project
  // directory is in effect at all (the field is empty AND no ancestor supplies
  // one, so effectiveProjectDir === ''). "(not in this project)" names a project
  // scope that is not set — incoherent to a reader looking at an empty Project
  // directory field — so a distinct "(not available)" wording is used and the
  // notice drops the "this directory's project agents" framing. Checked BEFORE
  // the in-a-project split because it is about the ABSENCE of a project, not
  // which one; "(not installed)" (a seed-equal saved agent at the seeded dir)
  // stays untouched, since that reads correctly with or without a directory.
  const orphanDirless = orphanIsRescopeOnly && !effectiveProjectDir
  const agentNames = effectiveAgents.map(a => a.name)
  // An unresolved EXPLICIT pick stays in the options so the trigger always names
  // what the user selected. An inherited effective agent is already represented
  // by the flagged clear label below and must not be duplicated as an explicit
  // pick. The orphan and scan-error arms carry different claims: a settled
  // roster proves absence, while a failed scan can only say it could not check.
  const pinnedAgent = draft.defaultAgent && (orphanAgent || unvalidatableAgent)
    ? draft.defaultAgent
    : ''
  const agentOptions = pinnedAgent ? [pinnedAgent, ...agentNames] : agentNames
  const agentOptionLabels = pinnedAgent
    ? [
        orphanAgent
          ? i18nT(
              orphanDirless
                ? 'components.folderConfigModal.agent_not_available'
                : orphanIsRescopeOnly
                  ? 'components.folderConfigModal.agent_not_in_project'
                  : 'components.folderConfigModal.agent_not_installed',
              { agent: orphanAgent },
            )
          : i18nT('components.folderConfigModal.agent_unverified', { agent: pinnedAgent }),
        ...agentNames,
      ]
    : agentNames
  const inheritedAgentLabel = inheritedAgent
    ? i18nT('components.folderConfigModal.inherit_named', { agent: inheritedAgent })
    : ''
  // When the effective orphan/unverifiable agent is inherited, the select's
  // value is still the empty (inherit) option. Compose the existing translated
  // suffix around that clear label so the trigger itself is visibly flagged;
  // otherwise only the notice below would reveal why Save is blocked.
  const effectiveInheritedAgentLabel = inheritedAgentLabel && !draft.defaultAgent
    ? orphanAgent
      ? orphanIsRescopeOnly && !orphanDirless
        ? i18nT('components.folderConfigModal.inherit_named_not_in_project', {
            agent: inheritedAgent,
          })
        : i18nT(
            orphanDirless
              ? 'components.folderConfigModal.inherit_named_not_available'
              : 'components.folderConfigModal.inherit_named_not_installed',
            { agent: inheritedAgent },
          )
      : unvalidatableAgent
        ? i18nT('components.folderConfigModal.inherit_named_unverified', { agent: inheritedAgent })
        : inheritedAgentLabel
    : inheritedAgentLabel

  const submit = useCallback(async () => {
    if (!canSubmit || saving) return
    const seeded = seedRef.current
    // THE tag-payload invariant: `tags` enters the PATCH only when the user
    // actually toggled a chip (draft differs from what this modal seeded).
    // A rename-only save must omit `tags` entirely — sending any list would
    // overwrite tags another client added to the folder while this modal sat
    // open. No client-side dangling-id prune is needed: the folder endpoint
    // silently filters unknown ids exactly like the slot-tags endpoint it
    // mirrors, so a stale reference is shed by the server on save and can
    // never 400 the folder.
    const tagsEdited = !sameTags(draft.tags, seeded.tags)
    const edited: FolderConfigField[] = []
    if (trimmedName !== seeded.name) edited.push('name')
    if (draft.color !== seeded.color) edited.push('color')
    // An armed regenerate is an icon edit too — the value looks unchanged
    // (Auto-generate restores the seeded emoji) but the user asked for a new
    // one, and the caller branches on regenerateIcon before touched('icon').
    if (draft.icon !== seeded.icon || draft.regenerateIcon) edited.push('icon')
    if (draft.projectDir !== seeded.projectDir) edited.push('projectDir')
    if (draft.defaultAgent !== seeded.defaultAgent) edited.push('defaultAgent')
    if (tagsEdited) edited.push('tags')
    if (!sameDirs(draft.steeringDirs, seeded.steeringDirs)) edited.push('steeringDirs')
    setSaving(true); setSaveErr(''); setIconErr(false)
    try {
      await onSubmit({ ...draft, name: trimmedName, touched: edited })
    } catch (e) {
      // Stay open, keep every field, and say why. An icon rejection is the one
      // failure with a field to point at: anchor it there, localized, instead
      // of echoing the server's English text in the top alert. Only
      // `icon_invalid` routes here — `regenerate_icon_invalid` is a request-
      // SHAPE error (non-boolean `regenerate_icon`, which this modal can never
      // send), and the field hint would misdescribe it.
      const code = e instanceof ApiError ? parseErrorCode(e.body) : undefined
      if (code === 'icon_invalid') {
        setIconErr(true)
      } else {
        setSaveErr(e instanceof Error && e.message ? e.message : i18nT('components.folderConfigModal.save_failed'))
      }
    } finally {
      setSaving(false)
    }
  }, [canSubmit, saving, draft, trimmedName, onSubmit])


  // The inline input this replaced held ONE field; the modal holds four, so an
  // accidental backdrop graze now costs real work. Guard the accidental paths
  // while the draft differs from what it opened with — Cancel and X still close.
  const seed = seedRef.current
  const touched: FolderConfigField[] = []
  if (draft.name !== seed.name) touched.push('name')
  if (draft.color !== seed.color) touched.push('color')
  if (draft.icon !== seed.icon || draft.regenerateIcon) touched.push('icon')
  if (draft.projectDir !== seed.projectDir) touched.push('projectDir')
  if (draft.defaultAgent !== seed.defaultAgent) touched.push('defaultAgent')
  if (!sameTags(draft.tags, seed.tags)) touched.push('tags')
  if (!sameDirs(draft.steeringDirs, seed.steeringDirs)) touched.push('steeringDirs')
  const isDirty = touched.length > 0

  return (
    <>
      <Modal
        open={open}
        onClose={onClose}
        guardAccidentalDismiss={isDirty || saving}
        maxWidth={480}
        title={mode === 'create' ? i18nT('components.folderConfigModal.new_folder') : i18nT('components.folderConfigModal.folder_settings')}
        footer={
          <>
            {/* Decorative only, not a live region: pressing Enter while Save is
                disabled does nothing, so the hint dims in step with the button
                rather than claiming it still works. Dimmed via the existing
                `text-muted` token (this modal's own less-prominent sibling of
                `text-muted-strong`, used elsewhere here for field labels) —
                never hidden, since removing the element would shift Cancel and
                Save (`mr-auto` stays the spacer either way). */}
            <span className={`mr-auto text-[11px] ${canSubmit ? 'text-muted-strong' : 'text-muted'}`}>{i18nT('components.folderConfigModal.enter_to_submit')}</span>
            <Btn onClick={onClose} disabled={saving}>{i18nT('components.folderConfigModal.cancel')}</Btn>
            <Btn primary disabled={!canSubmit || saving} data-testid="folder-config-submit" onClick={submit}>
              {mode === 'create' ? i18nT('components.folderConfigModal.create_folder') : i18nT('components.folderConfigModal.save_changes')}
            </Btn>
          </>
        }
      >
        <div className="flex flex-col gap-4">
          {/* No hand-off: the folder name / color / project dir / default agent /
              tags form is unsaved — the save that failed is exactly what the
              draft was about, and the navigation would discard it. */}
          <ErrorNotice message={saveErr} testId="folder-config-error" />

          {/* Read-only destination. Not an input: the entry point already fixed it. */}
          <div data-testid="folder-config-destination" className="flex items-center gap-1.5 flex-wrap text-[11.5px] text-muted bg-bg-accent border border-border rounded-lg px-3 py-2">
            <span className="text-text font-medium">{i18nT('components.folderConfigModal.top_level')}</span>
            {chain.map(f => (
              <span key={f.id} className="flex items-center gap-1.5">
                <ChevronRight size={11} className="text-muted-strong shrink-0" />
                <span className="text-text font-medium truncate max-w-[140px]">{f.name}</span>
              </span>
            ))}
            <ChevronRight size={11} className="text-muted-strong shrink-0" />
            <span className="text-accent font-semibold truncate max-w-[160px]">
              {trimmedName || (mode === 'create'
                ? i18nT('components.folderConfigModal.new_folder_leaf')
                : folder?.name)}
            </span>
          </div>

          {/* Name. The folder's identity mark is a palette color, applied to
           *  the swatch row below — there is no per-folder icon to preview,
           *  so the name input owns the full width. */}
          <label htmlFor="folder-config-name-input" className="flex flex-col gap-1.5">
            <span className="text-[11.5px] font-semibold text-muted">{i18nT('components.folderConfigModal.name')}</span>
            <Input
              ref={nameRef}
              id="folder-config-name-input"
              className="w-full"
              data-testid="folder-config-name"
              placeholder={i18nT('components.folderConfigModal.name_placeholder')}
              value={draft.name}
              onChange={e => setDraft(d => ({ ...d, name: e.target.value }))}
              {...ime.bindComposition()}
              onKeyDown={e => { if (e.key === 'Enter' && ime.claimEnter(e)) submit() }}
            />
          </label>

          {/* Color — always visible, compact. Leading "no color" swatch
           *  doubles as the remove affordance, so there is no separate reset
           *  control. */}
          <div className="flex flex-col gap-1.5">
            <span className="text-[11.5px] font-semibold text-muted">{i18nT('components.folderConfigModal.color')}</span>
            <div className="flex items-center gap-1 flex-wrap">
              <button
                type="button"
                data-testid="folder-config-color-reset"
                title={i18nT('components.folderConfigModal.no_color')}
                aria-label={i18nT('components.folderConfigModal.no_color')}
                aria-pressed={!draft.color}
                onClick={() => setDraft(d => ({ ...d, color: '' }))}
                className={`relative w-5 h-5 rounded-full cursor-pointer border overflow-hidden bg-bg-elevated border-border-strong ${!draft.color ? 'ring-1 ring-accent ring-offset-1 ring-offset-bg' : ''}`}
              >
                {/* diagonal slash = the universal "none" cell */}
                <span aria-hidden className="absolute left-1/2 top-1/2 w-[26px] h-px bg-danger -translate-x-1/2 -translate-y-1/2 rotate-45" />
              </button>
              {FOLDER_COLOR_PALETTE.map(({ value, label }) => {
                const name = label()
                return (
                  <button
                    key={value}
                    type="button"
                    title={i18nT('components.folderConfigModal.set_color_to_name', { name })}
                    aria-label={i18nT('components.folderConfigModal.set_color_to_name', { name })}
                    aria-pressed={draft.color === value}
                    onClick={() => setDraft(d => ({ ...d, color: value }))}
                    className={`w-5 h-5 rounded-full cursor-pointer border hover:brightness-125 swatch-cue ${draft.color === value ? 'ring-1 ring-accent ring-offset-1 ring-offset-bg' : ''}`}
                    style={{ background: `color-mix(in srgb, ${value} 30%, var(--bg-elevated))`, borderColor: value }}
                  />
                )
              })}
            </div>
          </div>

          {/* Icon — an emoji replacing the default folder glyph. Left empty,
           *  the folder keeps the default glyph — generation never runs
           *  implicitly; in edit mode "Auto-generate" asks for a fresh pick
           *  (regenerate_icon) while clearing the field falls back to the
           *  default glyph. Typing
           *  clears a pending regenerate and vice versa: the backend rejects
           *  icon + regenerate_icon in one request, so the two stay exclusive
           *  here. No client-side emoji validation — the server 400s on
           *  anything but a single emoji and the error renders above. */}
          <label htmlFor="folder-config-icon-input" className="flex flex-col gap-1.5">
            <span className="text-[11.5px] font-semibold text-muted">{i18nT('components.folderConfigModal.icon')}</span>
            <div className="flex flex-wrap items-center gap-2">
              <FolderGlyph
                color={draft.color || undefined}
                icon={draft.regenerateIcon ? '' : draft.icon || undefined}
                size={20}
                className="shrink-0 text-muted"
                testId="folder-config-icon-preview"
              />
              <Input
                id="folder-config-icon-input"
                className="w-24"
                data-testid="folder-config-icon"
                placeholder={i18nT('components.folderConfigModal.icon_placeholder')}
                maxLength={16}
                value={draft.regenerateIcon ? '' : draft.icon}
                onChange={e => { setIconErr(false); setDraft(d => ({ ...d, icon: e.target.value, regenerateIcon: false })) }}
              />
              {mode === 'edit' && (
                <Btn
                  data-testid="folder-config-icon-regenerate"
                  onClick={() => { setIconErr(false); setDraft(d => ({ ...d, icon: seedRef.current.icon, regenerateIcon: true })) }}
                >
                  {i18nT('components.folderConfigModal.icon_regenerate')}
                </Btn>
              )}
            </div>
            {iconErr ? (
              /* No hand-off: the rejected icon sits inside the same unsaved
                 folder form — navigating away would discard the whole draft
                 the keep-open-on-error path exists to preserve. The fix is a
                 one-field edit right here (type a single emoji or clear it). */
              <ErrorNotice
                variant="inline"
                className="text-[11px]"
                message={i18nT('components.folderConfigModal.icon_invalid_hint')}
                testId="folder-config-icon-error"
              />
            ) : (
              <span className="text-[11px] text-muted-strong">
                {draft.regenerateIcon
                  ? i18nT('components.folderConfigModal.icon_regenerate_pending')
                  : mode === 'create'
                    ? draft.icon
                      ? ''
                      : i18nT('components.folderConfigModal.icon_default_hint')
                    : draft.icon
                      ? ''
                      : i18nT('components.folderConfigModal.icon_cleared_hint')}
              </span>
            )}
          </label>

          {/* Tags — chips from the tag vocabulary, copied onto every new chat
           *  filed into this folder. Three vocabulary states, three renders:
           *  UNKNOWN (undefined, query unresolved/failed) renders nothing —
           *  showing the "create tags" hint would falsely tell a user who HAS
           *  tags that none exist; KNOWN-EMPTY shows the onboarding hint
           *  rather than vanishing — a hidden section makes the feature
           *  undiscoverable from the one place it lives; KNOWN-NON-EMPTY
           *  renders the picker. UNRESOLVED (query still loading) keeps the
           *  section heading with a muted placeholder instead of nothing, so
           *  the feature never silently vanishes and the layout does not
           *  shift when the vocabulary resolves after open. FAILED renders an
           *  error line, not the loading hint — a dead query must not assert
           *  an in-progress state indefinitely. */}
          {availableTags === undefined ? (
            <div className="flex flex-col gap-1.5">
              <span className="text-[11.5px] font-semibold text-muted">{i18nT('components.folderConfigModal.tags')}</span>
              {availableTagsFailed ? (
                <span className="flex items-center gap-1.5 flex-wrap">
                  {/* No hand-off: the same unsaved folder form (see the save
                      notice above) — a read failure, but it sits inside it. */}
                  <ErrorNotice
                    variant="inline"
                    className="text-[11px]"
                    message={i18nT('components.folderConfigModal.tags_error_hint')}
                    testId="folder-config-tags-error"
                  />
                  <button
                    type="button"
                    data-testid="folder-config-tags-retry"
                    onClick={onRetryTags}
                    className="text-[11px] underline underline-offset-2 text-danger hover:opacity-80 bg-transparent border-none p-0 cursor-pointer"
                  >
                    {i18nT('components.folderConfigModal.tags_retry')}
                  </button>
                </span>
              ) : (
                <span data-testid="folder-config-tags-loading" className="text-[11px] text-muted-strong">
                  {i18nT('components.folderConfigModal.tags_loading_hint')}
                </span>
              )}
            </div>
          ) : availableTags.length > 0 ? (
            <div className="flex flex-col gap-1.5">
              <span className="text-[11.5px] font-semibold text-muted">{i18nT('components.folderConfigModal.tags')}</span>
              <div data-testid="folder-config-tags" className="flex items-center gap-1.5 flex-wrap">
                {availableTags.map(tag => {
                  const selected = draft.tags.includes(tag.id)
                  return (
                    <label
                      key={tag.id}
                      htmlFor={`folder-config-tag-input-${tag.id}`}
                      data-testid={`folder-config-tag-${tag.id}`}
                      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11.5px] cursor-pointer hover:brightness-110 focus-within:ring-2 focus-within:ring-accent focus-within:ring-offset-1 focus-within:ring-offset-bg ${selected ? 'ring-1 ring-accent ring-offset-1 ring-offset-bg' : ''}`}
                      style={{
                        background: selected
                          ? `color-mix(in srgb, ${tag.color} 30%, var(--bg-elevated))`
                          : 'var(--bg-elevated)',
                        borderColor: tag.color,
                        color: 'var(--text)',
                      }}
                    >
                      {/* A hidden checkbox, not a <button>: the chips are a
                       *  multi-select choice control, and rendering them as
                       *  sibling buttons would read as an unbounded action row
                       *  (AUTOSDE max-two-buttons-per-row). The label supplies
                       *  the accessible name; checked state carries selection. */}
                      <input
                        type="checkbox"
                        id={`folder-config-tag-input-${tag.id}`}
                        aria-label={tag.name}
                        className="sr-only"
                        checked={selected}
                        onChange={() => setDraft(d => ({
                          ...d,
                          tags: selected ? d.tags.filter(t => t !== tag.id) : [...d.tags, tag.id],
                        }))}
                      />
                      <span aria-hidden className="w-2 h-2 rounded-full shrink-0" style={{ background: tag.color }} />
                      <span className="truncate max-w-[140px]">{tag.name}</span>
                      {/* Same "tag is on" glyph SlotTagPopover uses: selection
                       *  must not hinge on a 1px ring-width difference from the
                       *  keyboard-focus ring of the same accent color. */}
                      {selected && <span aria-hidden className="text-accent"><Check size={11} /></span>}
                    </label>
                  )
                })}
              </div>
              <span className="text-[11px] text-muted-strong">{i18nT('components.folderConfigModal.tags_hint')}</span>
            </div>
          ) : (
            <div className="flex flex-col gap-1.5">
              <span className="text-[11.5px] font-semibold text-muted">{i18nT('components.folderConfigModal.tags')}</span>
              <span data-testid="folder-config-tags-empty" className="text-[11px] text-muted-strong">
                {i18nT('components.folderConfigModal.tags_empty_hint')}
              </span>
            </div>
          )}

          {/* Project directory */}
          <div className="flex flex-col gap-1.5">
            <span className="text-[11.5px] font-semibold text-muted">{i18nT('components.folderConfigModal.project_directory')}</span>
            <div className="flex gap-2">
              <Input
                className="flex-1 min-w-0 font-mono text-[12px]"
                data-testid="folder-config-project-dir"
                aria-label={i18nT('components.folderConfigModal.project_directory')}
                placeholder={inheritedDir
                  ? i18nT('components.folderConfigModal.inherited_placeholder', { path: inheritedDir })
                  : i18nT('components.folderConfigModal.project_dir_placeholder')}
                value={draft.projectDir}
                onChange={e => setDraft(d => ({ ...d, projectDir: e.target.value }))}
                {...ime.bindComposition({
                  onFocus: () => setProjectDirFieldActive(true),
                  // Blur ends this field's edit even mid-composition (the IME
                  // guard's own reset already fires here) — the flag defers
                  // until the field is committed one way or another, and losing
                  // focus is as much a commit signal as Enter.
                  onBlur: () => setProjectDirFieldActive(false),
                })}
                onKeyDown={e => {
                  if (e.key === 'Enter' && ime.claimEnter(e)) { setProjectDirFieldActive(false); submit() }
                }}
              />
              <Btn ref={browseRef} data-testid="folder-config-browse" onClick={() => { setPickerTarget('project'); setPickerOpen(true) }}>
                <FolderOpen size={13} /> {i18nT('components.folderConfigModal.browse')}
              </Btn>
            </div>
            {!draft.projectDir && inheritedDir ? (
              <span className="text-[11px] text-muted-strong">{i18nT('components.folderConfigModal.inherited_dir')}</span>
            ) : (
              <span className="text-[11px] text-muted-strong">{i18nT('components.folderConfigModal.project_dir_hint')}</span>
            )}
          </div>

          {/* Extra steering directories — an ordered list of directory rows the
           *  user builds with the SAME ProjectPicker the project-dir field uses
           *  (routed through pickerTarget='steering'). Ancestor dirs accumulate
           *  and are shown read-only below, so the user sees the full effective
           *  set without being able to edit a parent's contribution here. */}
          <div className="flex flex-col gap-1.5">
            <span className="text-[11.5px] font-semibold text-muted">{i18nT('components.folderConfigModal.steering_dirs')}</span>
            {draft.steeringDirs.length > 0 && (
              <div data-testid="folder-config-steering-dirs" className="flex flex-col gap-1.5">
                {draft.steeringDirs.map((dir, i) => (
                  <div
                    key={`${dir}-${i}`}
                    data-testid={`folder-config-steering-dir-${i}`}
                    className="flex items-center gap-2 bg-bg-elevated border border-border rounded-lg px-2.5 py-1.5"
                  >
                    <FolderOpen size={12} className="text-accent shrink-0" />
                    <span className="flex-1 min-w-0 font-mono text-[12px] text-text truncate" title={dir}>{dir}</span>
                    <button
                      type="button"
                      data-testid={`folder-config-steering-dir-remove-${i}`}
                      aria-label={i18nT('components.folderConfigModal.steering_dir_remove', { path: dir })}
                      onClick={() => setDraft(d => ({ ...d, steeringDirs: d.steeringDirs.filter((_, j) => j !== i) }))}
                      className="shrink-0 p-0.5 text-muted hover:text-danger rounded hover:bg-bg-hover cursor-pointer bg-transparent border-none"
                    >
                      <X size={13} />
                    </button>
                  </div>
                ))}
              </div>
            )}
            <div>
              <Btn ref={steeringBrowseRef} data-testid="folder-config-steering-add" onClick={() => { setPickerTarget('steering'); setPickerOpen(true) }}>
                <Plus size={13} /> {i18nT('components.folderConfigModal.steering_dir_add')}
              </Btn>
            </div>
            {inheritedSteeringDirs.length > 0 && (
              <div data-testid="folder-config-steering-inherited" className="flex flex-col gap-1">
                <span className="text-[11px] font-semibold text-muted-strong">{i18nT('components.folderConfigModal.steering_dirs_inherited')}</span>
                {inheritedSteeringDirs.map((dir, i) => (
                  <div key={`inh-${dir}-${i}`} className="flex items-center gap-2 opacity-60 px-2.5 py-1">
                    <FolderOpen size={12} className="text-muted shrink-0" />
                    <span className="flex-1 min-w-0 font-mono text-[12px] text-muted truncate" title={dir}>{dir}</span>
                  </div>
                ))}
              </div>
            )}
            <span className="text-[11px] text-muted-strong">{i18nT('components.folderConfigModal.steering_dirs_hint')}</span>
          </div>

          {/* Default agent. SimpleSelect renders a <button>, not a <select>, so
           *  this block is a plain div like the project-directory one above and
           *  the heading's own key doubles as the control's aria-label — an
           *  external <label htmlFor> cannot associate with it. Its popup
           *  portals at z-[9999], above the modal's z-[101], the same way
           *  ProjectPicker's does below. */}
          <div className="flex flex-col gap-1.5">
            <span className="flex items-center gap-1.5 text-[11.5px] font-semibold text-muted">
              <Zap size={12} className="shrink-0" /> {i18nT('components.folderConfigModal.default_agent')}
            </span>
            <SimpleSelect
              aria-label={i18nT('components.folderConfigModal.default_agent')}
              // Bind the orphan notice to the control so a screen reader reaches
              // the reason WITH the field, not as text that merely sits near it:
              // a control whose state has a cause the user cannot hear is the
              // same defect as a disabled button that never says why. Only while
              // an orphan is selected — an ordinary selection has nothing to
              // describe, and a dangling id here would drop the description.
              aria-describedby={orphanAgent ? 'folder-config-agent-notice' : undefined}
              options={agentOptions}
              optionLabels={agentOptionLabels}
              clearLabel={effectiveInheritedAgentLabel
                || i18nT('components.folderConfigModal.none')}
              value={draft.defaultAgent}
              onChange={v => setDraft(d => ({ ...d, defaultAgent: v }))}
            />
            {orphanAgent ? (
              // The orphan is round-tripped, not blocked — Save stays enabled so
              // a rename of the folder never wipes a temporarily-uninstalled
              // agent (the round-trip guarantee this picker was built on). The
              // notice therefore explains why the SELECTED AGENT will not run and
              // names the fix. Its id is what `aria-describedby` above targets.
              <span
                id="folder-config-agent-notice"
                data-testid="folder-config-agent-notice"
                className="text-[11px] text-warn"
              >
                {/* Keyed on the SAME predicate as the trigger's label, because the
                    two sit one line apart and must not contradict each other. The
                    installed-notice wording is false in the rescope case on all
                    three counts: the agent IS installed, "pick an installed agent"
                    is the wrong advice when changing the directory is the other
                    fix, and "chats here won't use it" is the wrong consequence
                    when Save is blocked outright. */}
                {i18nT(
                  orphanDirless
                    ? 'components.folderConfigModal.agent_not_available_notice'
                    : orphanIsRescopeOnly
                      ? 'components.folderConfigModal.agent_not_in_project_notice'
                      : 'components.folderConfigModal.agent_not_installed_notice',
                )}
              </span>
            ) : rosterScanError ? (
              // While the scan-error row (below) is showing, the roster is
              // unknown and Save is dead for it. The pre-selection hint —
              // "Pre-selected for new chats created here." — promises exactly
              // the behaviour that row explains is broken, sitting directly
              // above "Couldn't read this directory's agents.", so suppress it
              // and let the error row speak alone.
              null
            ) : (
              <span className="text-[11px] text-muted-strong">{i18nT('components.folderConfigModal.default_agent_hint')}</span>
            )}
            {rosterInFlight ? (
              /* Pending outranks a stale error during an explicit refetch. React
                 Query may preserve isError while isFetching, and leaving the
                 error row first makes Retry look inert until the next outcome. */
              <span data-testid="folder-config-agent-roster-loading" className="text-[11px] text-muted-strong">
                {i18nT('components.folderConfigModal.agent_roster_loading')}
              </span>
            ) : rosterScanError ? (
              <span ref={rosterErrorRef} className="flex items-center gap-1.5 flex-wrap">
                {/* No hand-off: the same unsaved folder form. The scan failed,
                    so distinguish "couldn't read" from "no project agents", and
                    offer a retry rather than leaving Save silently blocked. */}
                <ErrorNotice
                  variant="inline"
                  className="text-[11px]"
                  message={i18nT('components.folderConfigModal.agent_roster_error')}
                  testId="folder-config-agent-roster-error"
                />
                <button
                  type="button"
                  data-testid="folder-config-agent-roster-retry"
                  onClick={() => projectRosterQuery.refetch()}
                  className="text-[11px] underline underline-offset-2 text-danger hover:opacity-80 bg-transparent border-none p-0 cursor-pointer"
                >
                  {i18nT('components.folderConfigModal.agent_roster_retry')}
                </button>
              </span>
            ) : null}
          </div>
        </div>
      </Modal>

      {/* Portals at z-[9999], above the modal's z-[101], and anchors to the
       *  Browse button. Reused rather than reimplemented so folder-directory
       *  picking stays identical to every other project-directory picker. */}
      {pickerOpen && (
        <ProjectPicker
          open={true}
          onOpenChange={o => { if (!o) setPickerOpen(false) }}
          anchorRef={pickerTarget === 'steering' ? steeringBrowseRef : browseRef}
          onSelect={path => {
            setDraft(d => pickerTarget === 'steering'
              // Append to the ordered list, ignoring a path already present so a
              // double-pick cannot duplicate a row.
              ? (d.steeringDirs.includes(path) ? d : { ...d, steeringDirs: [...d.steeringDirs, path] })
              : { ...d, projectDir: path })
            // A picker pick is a deliberate, complete commit of the project
            // directory — not a mid-typing pause — so it ends the same "field
            // is active" span the text input's blur/Enter ends.
            if (pickerTarget === 'project') setProjectDirFieldActive(false)
            setPickerOpen(false)
          }}
        />
      )}
    </>
  )
}
