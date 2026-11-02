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
import { compareText } from '../i18n/format'
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
      })
    }
  }
  return out.sort((a, b) => compareText(a.appLabel, b.appLabel))
}
