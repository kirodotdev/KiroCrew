/**
 * App-contributed selection actions (`contributes.selectionActions`): discovery.
 *
 * The manifest is validated server-side; this module re-checks only what it needs
 * to offer an item safely, because the app list it reads may be stale. An item is
 * offered only when the row's `selectionActionGrant.approved` names it: the owner
 * approved exactly what it now declares (`apps/selection_action_grants.py`). A row
 * without that key offers nothing.
 *
 * @module selectionActions/selectionActionRegistry
 */
import type { OfferKind } from '../appFrame/frameProtocol'
import { compareText, fmtNumber } from '../i18n/format'
import { i18nT } from '../i18n/t'
import type { SelectionSourceKind } from './selectionSources'

/** Mirrors `_PANEL_TAB_ID_RE` and the caps in `apps/manifest_contributes.py`. */
const ID_RE = /^[a-z][a-z0-9_-]{0,63}$/
export const MAX_SELECTION_TEXT = 8000
const MAX_TITLE = 40
const MAX_PER_APP = 2

export interface ResolvedSelectionAction {
  /** `${appName}:${id}`. */
  key: string
  appName: string
  /** The app's display name, for attribution in host chrome. */
  appLabel: string
  id: string
  title: string
  icon: string
  /** ESM path relative to the app's `ui/` dir. */
  entry: string
  sources: ReadonlySet<SelectionSourceKind>
  minLength: number
  maxLength: number
  effects: ReadonlySet<OfferKind>
  width: number
  maxHeight: number
  /** Which install of the module this is; a frame for another revision is not reused. */
  revision: string
  /** The grant entry this item was resolved at. Every mint names it, so the gateway
   *  refuses a pick made against an item the owner has since changed. */
  grant: string
}

interface AppLike {
  name?: string
  displayName?: string
  enabled?: boolean
  version?: string
  installedAt?: string
  updatedAt?: string
  selectionActionGrant?: { approved?: unknown }
  manifest?: { displayName?: string; contributes?: { selectionActions?: unknown } }
}

/** The grant entry of an item. Mirrors `selection_action_entry` in `apps/selection_action_grants.py`. */
export function selectionGrantEntry(item: { id: string; sources: readonly string[]; minLength: number; maxLength: number; effects: readonly string[] }): string {
  const sorted = (xs: readonly string[]) => [...new Set(xs)].sort().join(',')
  return `${item.id}:${sorted(item.sources)}:${item.minLength}-${item.maxLength}:${sorted(item.effects)}`
}

/**
 * One held-back grant entry (`<id>:<sources>:<min>-<max>:<effects>`) in the
 * owner's words: the item's title from the manifest, how much selected text it
 * gets and which host buttons it may fill. An entry this reader cannot parse is
 * shown as it is, so the owner never approves something the label hides.
 */
export function selectionActionGrantLabel(entry: string, declared: unknown): string {
  const m = /^([^:]+):([^:]*):(\d+)-(\d+):([^:]*)$/.exec(entry)
  if (!m) return entry
  const [, id, sources, , max, effects] = m
  if (sources !== 'chat') return entry
  const fx = effects ? effects.split(',') : []
  if (!fx.every((e) => e === 'copy' || e === 'open')) return entry
  const decl = Array.isArray(declared)
    ? (declared.find((d) => d && typeof d === 'object' && (d as Record<string, unknown>).id === id) as Record<string, unknown> | undefined)
    : undefined
  const title = typeof decl?.title === 'string' && decl.title ? decl.title : id
  const vars = { title, max: fmtNumber(Number(max)) }
  const copy = fx.includes('copy')
  const open = fx.includes('open')
  if (copy && open) return i18nT('components.selectionActions.grant_entry_copy_open', vars)
  if (copy) return i18nT('components.selectionActions.grant_entry_copy', vars)
  if (open) return i18nT('components.selectionActions.grant_entry_open', vars)
  return i18nT('components.selectionActions.grant_entry', vars)
}

const intIn = (v: unknown, lo: number, hi: number, dflt: number): number | null => {
  if (v === undefined) return dflt
  return typeof v === 'number' && Number.isInteger(v) && v >= lo && v <= hi ? v : null
}

const strings = (v: unknown, dflt: string[]): string[] | null =>
  v === undefined ? dflt : Array.isArray(v) && v.every((x) => typeof x === 'string') ? (v as string[]) : null

/** Every enabled app's approved selection actions, ordered by app name then declaration. */
export function resolveSelectionActions(apps: readonly AppLike[]): ResolvedSelectionAction[] {
  const out: ResolvedSelectionAction[] = []
  for (const app of apps) {
    if (!app || typeof app.name !== 'string' || !app.name) continue
    if (app.enabled === false) continue
    const declared = app.manifest?.contributes?.selectionActions
    const approved = app.selectionActionGrant?.approved
    if (!Array.isArray(declared) || !Array.isArray(approved)) continue
    const approvedIds = new Set(approved.filter((x): x is string => typeof x === 'string'))
    for (const raw of declared.slice(0, MAX_PER_APP)) {
      if (!raw || typeof raw !== 'object') continue
      const p = raw as Record<string, unknown>
      const id = typeof p.id === 'string' ? p.id : ''
      const title = typeof p.title === 'string' ? p.title : ''
      const entry = typeof p.entry === 'string' ? p.entry : ''
      if (!ID_RE.test(id) || !approvedIds.has(id) || !title || title.length > MAX_TITLE) continue
      if (!entry || entry.includes('..') || entry.startsWith('/')) continue
      const sources = strings(p.sources, ['chat'])
      const effects = strings(p.effects, [])
      const minLength = intIn(p.minLength, 1, MAX_SELECTION_TEXT, 1)
      const maxLength = intIn(p.maxLength, 1, MAX_SELECTION_TEXT, MAX_SELECTION_TEXT)
      const popover = (p.popover && typeof p.popover === 'object' ? p.popover : {}) as Record<string, unknown>
      const width = intIn(popover.width, 200, 480, 360)
      const maxHeight = intIn(popover.maxHeight, 120, 600, 480)
      if (!sources || !effects || minLength === null || maxLength === null || minLength > maxLength || width === null || maxHeight === null) continue
      if (!sources.every((s) => s === 'chat') || !effects.every((e) => e === 'copy' || e === 'open')) continue
      out.push({
        key: `${app.name}:${id}`,
        appName: app.name,
        appLabel: app.displayName || app.manifest?.displayName || app.name,
        id,
        title,
        icon: typeof p.icon === 'string' ? p.icon : '',
        entry: entry.replace(/^\.?\//, ''),
        sources: new Set(sources as SelectionSourceKind[]),
        minLength,
        maxLength,
        effects: new Set(effects as OfferKind[]),
        width,
        maxHeight,
        revision: JSON.stringify([entry, app.version ?? '', app.installedAt ?? '', app.updatedAt ?? '']),
        grant: selectionGrantEntry({ id, sources, minLength, maxLength, effects }),
      })
    }
  }
  return out.sort((a, b) => compareText(a.appLabel, b.appLabel))
}
