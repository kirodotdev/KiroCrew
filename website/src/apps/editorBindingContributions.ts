/**
 * Code-editor bindings contributed by installed apps.
 *
 * This is what lets a key chord or a modifier-click in the side panel's code editor
 * belong to an app OUTSIDE this repository. An app declares `contributes.editorBindings`
 * in its manifest; this module turns that declaration into triggers the host matches
 * against the editor's own key and pointer events, and a match POSTs the file's PATH
 * and the caret to the app's own endpoint. Nothing here executes app code: a
 * contribution is data, and the host is the only thing that acts on it.
 *
 * It mirrors `fileMenuContributions.tsx` deliberately, down to re-validating what the
 * backend already checked: a manifest reaches this dashboard through the `['apps']`
 * query, which an older gateway or an edited `app.json` can fill with anything, so
 * manifest data from a third party is untrusted input. A bad declaration is SKIPPED
 * with a warning, never thrown.
 *
 * Kept pure (no React, no DOM) so the rules are pinned by unit test.
 */
import {
  api,
  type EditorBindingContext,
  type EditorBindingLocation,
  type EditorBindingResult,
} from '../api/client'
import { store } from '../store'
import { safeHttpUrl } from '../lib/safeUrl'
import { sha256Hex } from '../utils/sha256Hex'
import { pathBasename } from '../utils/pathBasename'
import { isMac } from '../utils/platform'
import { endpointAllowed } from './fileMenuContributions'
import {
  MODIFIERS,
  canonicalTrigger,
  dashboardShortcutFor,
  triggerProblem,
  type BindingModifier,
  type EditorBindingTrigger,
} from './editorBindingTriggers'

// The trigger grammar lives in `editorBindingTriggers.ts` (no API behind it, so the
// editor can import it); re-exported so readers of this module see one surface.
export { canonicalTrigger, triggerOfEvent } from './editorBindingTriggers'
export type { BindingEvent, BindingModifier, EditorBindingTrigger } from './editorBindingTriggers'

/** The subset of `GET /api/apps` this module reads. */
export interface EditorBindingAppRecord {
  name: string
  enabled?: boolean
  manifest?: {
    permissions?: { editor?: unknown }
    contributes?: {
      editorBindings?: unknown
    }
  }
  /** The owner-approved grant set, present once one was stored; absent approves the manifest. */
  approvedGrants?: { editor?: unknown }
}

/** A validated binding, ready to match. */
export interface ContributedEditorBinding {
  id: string
  /** The app that contributed it: namespaces the id and scopes the endpoint. */
  app: string
  /** App-owned literal, for the file menu's listing and a screen reader. */
  label: string
  trigger: EditorBindingTrigger
  endpoint: string
  when: { extensions: string[] }
  /**
   * Whether the buffer's text may follow the hash once the app says `stale`:
   * the app holds `permissions.editor: ["content"]`, approved by the owner.
   */
  textGranted: boolean
}

/** Mirrors `EDITOR_PERMISSION_CONTENT` in `apps/manifest_contributes.py`. */
const EDITOR_PERMISSION_CONTENT = 'content'

/**
 * Whether the app may be sent the reader's text. The approved set wins when one
 * is stored (an update that adds the grant is held back until the owner approves
 * it on the app page); with none stored the manifest's declaration stands, as it
 * does for `api` and `events`.
 */
export function editorTextGranted(app: EditorBindingAppRecord): boolean {
  const granted = app.approvedGrants ? app.approvedGrants.editor : app.manifest?.permissions?.editor
  return Array.isArray(granted) && granted.includes(EDITOR_PERMISSION_CONTENT)
}

/** Mirrors `_MAX_EDITOR_BINDINGS_PER_APP` in `apps/manifest_contributes.py`. */
export const MAX_EDITOR_BINDINGS_PER_APP = 12
/** Mirrors `_MAX_TITLE`. */
const MAX_LABEL = 120
/** Mirrors `_COMMAND_SLUG_RE`, which contributed ids share. */
const ITEM_ID_RE = /^[a-z0-9][a-z0-9-]*$/

function str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

/**
 * Why a contribution was skipped, on the console: refused silently it is invisible,
 * and the reason names the manifest field that failed, addressed to the app's author.
 * The same shim, under the same name, as `fileMenuContributions.tsx` and
 * `contributedCommands.ts`; the i18n gate exempts this callee rather than the file.
 */
function warnContributionSkipped(appName: string, id: unknown, reason: string): void {
  // eslint-disable-next-line no-console -- a refused contribution is invisible otherwise
  console.warn(
    `[editorBindingContributions] app ${appName}: skipping binding ${String(id)} — ${reason}`,
  )
}

/** Read a trigger, or say on the console why it was refused and return null. */
function readTrigger(app: string, id: string, raw: unknown): EditorBindingTrigger | null {
  if (typeof raw !== 'object' || raw === null) {
    warnContributionSkipped(app, id, 'trigger must be an object')
    return null
  }
  const obj = raw as Record<string, unknown>
  const key = str(obj.key).toLowerCase()
  const click = obj.click === true
  if (!key === !click) {
    warnContributionSkipped(app, id, 'trigger must name exactly one of key or click')
    return null
  }
  if (obj.modifiers !== undefined && !Array.isArray(obj.modifiers)) {
    warnContributionSkipped(app, id, 'trigger.modifiers must be an array')
    return null
  }
  const seen = new Set<BindingModifier>()
  for (const m of Array.isArray(obj.modifiers) ? obj.modifiers : []) {
    const mod = str(m).toLowerCase()
    if (!MODIFIERS.has(mod as BindingModifier)) {
      warnContributionSkipped(app, id, `unknown modifier ${mod}`)
      return null
    }
    seen.add(mod as BindingModifier)
  }
  const modifiers = [...seen].sort()
  const problem = triggerProblem(key, click, modifiers)
  const canonical = canonicalTrigger(key, click, modifiers)
  if (problem === 'key-or-click') {
    warnContributionSkipped(app, id, 'trigger must name exactly one of key or click')
    return null
  }
  if (problem === 'click-needs-modifier') {
    warnContributionSkipped(app, id, 'a click trigger needs at least one modifier')
    return null
  }
  if (problem === 'bad-key') {
    warnContributionSkipped(app, id, 'trigger.key must be F1-F12, a letter or a digit')
    return null
  }
  if (problem === 'key-needs-modifier') {
    warnContributionSkipped(app, id, 'a letter or digit trigger needs at least one modifier')
    return null
  }
  if (problem === 'reserved') {
    warnContributionSkipped(app, id, `${canonical} is the host's own chord`)
    return null
  }
  const shortcut = dashboardShortcutFor(canonical, isMac)
  if (shortcut !== null) {
    warnContributionSkipped(app, id, `${canonical} is the dashboard's ${shortcut} shortcut`)
    return null
  }
  return { key, click, modifiers, canonical }
}

function readBinding(app: EditorBindingAppRecord, raw: unknown): ContributedEditorBinding | null {
  if (typeof raw !== 'object' || raw === null) {
    warnContributionSkipped(app.name, raw, 'entry is not an object')
    return null
  }
  const obj = raw as Record<string, unknown>
  const id = str(obj.id)
  if (!ITEM_ID_RE.test(id)) {
    warnContributionSkipped(app.name, obj.id, 'id must be lowercase alphanumeric with dashes')
    return null
  }
  const label = str(obj.label)
  if (!label) {
    warnContributionSkipped(app.name, id, 'missing label')
    return null
  }
  if (label.length > MAX_LABEL) {
    warnContributionSkipped(app.name, id, `label exceeds ${MAX_LABEL} characters`)
    return null
  }
  const endpoint = str(obj.endpoint)
  if (!endpointAllowed(app.name, endpoint)) {
    warnContributionSkipped(app.name, id, `endpoint must route under /api/apps/${app.name}/`)
    return null
  }
  const trigger = readTrigger(app.name, id, obj.trigger)
  if (!trigger) return null
  // `when` is advisory: a malformed filter narrows nothing, and the manifest reports
  // the same input as an error, which is where an author finds out.
  const rawWhen = typeof obj.when === 'object' && obj.when !== null ? (obj.when as Record<string, unknown>) : {}
  const extensions = Array.isArray(rawWhen.extensions)
    ? rawWhen.extensions.filter((e): e is string => typeof e === 'string' && e.length > 0)
        .map(e => e.toLowerCase().replace(/^\.+/, ''))
    : []
  return { id, app: app.name, label, trigger, endpoint, when: { extensions }, textGranted: editorTextGranted(app) }
}

/**
 * Every valid binding contributed by the ENABLED installed apps, in app-name order.
 *
 * Disabled apps contribute nothing, for the reason the file-menu rows give: the
 * enable switch is the reader's switch for the whole app. Within one app a second
 * binding on a chord already bound is skipped (the manifest refuses it; this is the
 * dispatch-time floor). Across apps the order is the sorted app name, so when two
 * enabled apps bind one chord the same app wins on every machine.
 */
export function contributedEditorBindings(
  apps: readonly EditorBindingAppRecord[],
): ContributedEditorBinding[] {
  const out: ContributedEditorBinding[] = []
  const sorted = [...(Array.isArray(apps) ? apps : [])].sort((a, b) => (a.name ?? '').localeCompare(b.name ?? ''))
  for (const app of sorted) {
    if (!app.enabled) continue
    if (!app.name) {
      warnContributionSkipped('(unnamed)', '(all)', 'app record has no name')
      continue
    }
    const raw = app.manifest?.contributes?.editorBindings
    if (raw === undefined || raw === null) continue
    if (!Array.isArray(raw)) {
      warnContributionSkipped(app.name, '(all)', 'contributes.editorBindings is not an array')
      continue
    }
    const ids = new Set<string>()
    const chords = new Set<string>()
    // Sliced BEFORE the loop so the cap bounds the work, not just the output.
    for (const entry of raw.slice(0, MAX_EDITOR_BINDINGS_PER_APP)) {
      const binding = readBinding(app, entry)
      if (!binding) continue
      if (ids.has(binding.id)) {
        warnContributionSkipped(app.name, binding.id, 'duplicate id')
        continue
      }
      if (chords.has(binding.trigger.canonical)) {
        warnContributionSkipped(app.name, binding.id, `${binding.trigger.canonical} is already bound by this app`)
        continue
      }
      ids.add(binding.id)
      chords.add(binding.trigger.canonical)
      out.push(binding)
    }
  }
  return out
}

/** Whether a binding's `when` admits this file. A path with no extension matches no filter. */
export function bindingMatchesFile(binding: ContributedEditorBinding, path: string): boolean {
  const { extensions } = binding.when
  if (!extensions.length) return true
  const base = path.split('/').pop() ?? ''
  const dot = base.lastIndexOf('.')
  if (dot <= 0) return false
  return extensions.includes(base.slice(dot + 1).toLowerCase())
}

/** The binding bound to `trigger` for this file, if an enabled app declared one. */
export function bindingFor(
  bindings: readonly ContributedEditorBinding[],
  trigger: string,
  path: string,
): ContributedEditorBinding | null {
  return bindings.find(g => g.trigger.canonical === trigger && bindingMatchesFile(g, path)) ?? null
}

/** A row of the picker the host shows when a binding answers with several locations. */
export interface BindingCardRow {
  key: string
  path: string
  /** 1-based. */
  line: number
  character?: number
  sameFile: boolean
  primary: string
  secondary: string
}

/**
 * What the host's result card shows for a binding, from the moment it fires. The
 * card is the host's: an app supplies locations or markdown and never a component.
 */
export type BindingCardView =
  | { status: 'loading'; title: string }
  | { status: 'ready'; title: string; rows: BindingCardRow[]; markdown: string }

/** How the host acts on an endpoint's answer, and where a failure is reported. */
export interface BindingDispatchHost {
  /** The buffer as the reader sees it; hashed for every request, sent only after `stale`. */
  text: () => string
  /** Move the editor to a location: in this file, or by opening another. */
  open: (location: { path: string; line: number; character?: number }, sameFile: boolean) => void
  /** Replace the result card over the editor; `null` closes it. */
  show: (view: BindingCardView | null) => void
  /** The app's copy is behind the buffer and the binding may not be sent the text. */
  onStale: () => void
  /** The call failed: the error as thrown, for the host to name the app and the cause. */
  onError: (error: unknown) => void
}

/** SHA-256 of the buffer, through the platform digest where there is one. */
async function digest(text: string): Promise<string> {
  const subtle = globalThis.crypto?.subtle
  if (!subtle) return sha256Hex(text)
  const bytes = await subtle.digest('sha-256', new TextEncoder().encode(text))
  return Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, '0')).join('')
}

function readLocation(raw: unknown): EditorBindingLocation | null {
  if (!raw || typeof raw !== 'object') return null
  const loc = raw as Record<string, unknown>
  if (typeof loc.path !== 'string' || !loc.path) return null
  if (typeof loc.line !== 'number' || !Number.isInteger(loc.line) || loc.line < 1) return null
  const character = typeof loc.character === 'number' && Number.isInteger(loc.character) && loc.character >= 0
    ? loc.character
    : undefined
  return {
    path: loc.path,
    line: loc.line,
    character,
    label: typeof loc.label === 'string' && loc.label.trim() ? loc.label : undefined,
    detail: typeof loc.detail === 'string' ? loc.detail : undefined,
  }
}

/** The valid locations of an answer's `open`, one or many, in the app's order. */
export function locationsOf(result: EditorBindingResult): EditorBindingLocation[] {
  const raw = result.open
  const list = Array.isArray(raw) ? raw : raw === undefined || raw === null ? [] : [raw]
  return list.map(readLocation).filter((l): l is EditorBindingLocation => l !== null)
}

/** The picker's rows for several locations, labelled by the app or by `file:line`. */
export function rowsOf(locations: readonly EditorBindingLocation[], document: string): BindingCardRow[] {
  return locations.map((loc, i) => ({
    key: `${i}:${loc.path}:${loc.line}:${loc.character ?? ''}`,
    path: loc.path,
    line: loc.line,
    character: loc.character,
    sameFile: loc.path === document,
    primary: loc.label ?? `${pathBasename(loc.path)}:${loc.line}`,
    secondary: loc.detail ?? '',
  }))
}

/**
 * POST a binding to the app that declared it and act on the answer.
 *
 * The PATH, the caret and a HASH of the buffer cross the boundary; the text itself
 * only on the one retry after the app answered `stale`, and only for an app the
 * owner granted `permissions.editor: ["content"]`. A binding needs no permission to
 * exist, so shipping the bytes with every keystroke would hand them to any app that
 * declares one; the grant is what the app page shows and approves.
 *
 * One location opens at once. Several, or markdown, fill the host's card, which the
 * host already showed as loading when the binding fired, so a slow answer has a
 * visible owner and Escape has something to close.
 */
export async function invokeEditorBinding(
  binding: ContributedEditorBinding,
  at: { path: string; line: number; character: number; dirty: boolean; trigger: string },
  host: BindingDispatchHost,
): Promise<void> {
  const slot = store.getState().chat.activeSlot
  const sessionKey = slot ? `dashboard:${slot}` : undefined
  const title = binding.label
  host.show({ status: 'loading', title })
  try {
    const text = host.text()
    const ctx: EditorBindingContext = { ...at, sha256: await digest(text) }
    let result: EditorBindingResult | null | undefined = await api.invokeEditorBinding(binding, ctx, sessionKey)
    if (result && typeof result === 'object' && result.stale === true) {
      if (!binding.textGranted) {
        host.show(null)
        host.onStale()
        return
      }
      result = await api.invokeEditorBinding(binding, { ...ctx, text }, sessionKey)
    }
    if (!result || typeof result !== 'object') { host.show(null); return }
    const heading = typeof result.title === 'string' && result.title.trim() ? result.title : title
    const locations = locationsOf(result)
    const markdown = typeof result.markdown === 'string' ? result.markdown : ''
    if (locations.length === 1 && !markdown) {
      const [loc] = locations
      host.show(null)
      host.open({ path: loc.path, line: loc.line, character: loc.character }, loc.path === at.path)
    } else if (locations.length > 0 || markdown) {
      host.show({ status: 'ready', title: heading, rows: rowsOf(locations, at.path), markdown })
    } else {
      host.show(null)
    }
    if (typeof result.message !== 'string' || !result.message.trim()) return
    const url = typeof result.url === 'string' ? safeHttpUrl(result.url) : null
    window.dispatchEvent(new CustomEvent('mc:notify', {
      detail: { source: 'editor-binding', message: result.message, url: url ?? undefined, type: 'success' },
    }))
  } catch (err: unknown) {
    host.show(null)
    host.onError(err)
  }
}
