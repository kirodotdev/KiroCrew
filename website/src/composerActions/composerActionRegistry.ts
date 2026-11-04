/**
 * App-contributed composer actions: which buttons the composer's "+" menu shows.
 *
 * Apps declare them in `app.json` under `contributes.composerActions[]`. The gateway
 * validates the manifest and puts only the actions the owner APPROVED on the app
 * row (`composerActionGrant.actions`, `apps/composer_action_grants.py`); an action an
 * update added waits in `composerActionGrant.staged`. This module re-checks the
 * shape it renders, because the list may be stale or hand-edited.
 *
 * @module composerActions/composerActionRegistry
 */

/** Mirrors the caps in `apps/manifest_contributes.py`. */
export const MAX_COMPOSER_ACTIONS_PER_APP = 2
export const MAX_COMPOSER_ACTION_TITLE = 40
const ID_RE = /^[a-z][a-z0-9_-]{0,63}$/
const ICON_RE = /^[A-Z][A-Za-z0-9]{0,39}$/
const POPOVER_WIDTH = { min: 200, max: 480, fallback: 360 }
const POPOVER_HEIGHT = { min: 120, max: 600, fallback: 320 }

/** The grant entry of one action. Mirrors `action_grant` in `apps/composer_action_grants.py`. */
export function actionGrant(id: string): string {
  return `${id}:insert`
}

export interface ComposerActionItem {
  /** `${appName}:${id}`. */
  key: string
  appName: string
  /** The app's display name, for attribution next to the row and in the popover. */
  appLabel: string
  id: string
  title: string
  icon: string
  width: number
  maxHeight: number
}

interface AppLike {
  name?: string
  displayName?: string
  enabled?: boolean
  composerActionGrant?: { actions?: unknown }
  manifest?: { displayName?: string }
}

const clamp = (n: unknown, { min, max, fallback }: { min: number; max: number; fallback: number }): number =>
  typeof n === 'number' && Number.isInteger(n) && n >= min && n <= max ? n : fallback

/** Every enabled app's approved composer actions, in app-list order. */
export function resolveComposerActions(apps: readonly AppLike[]): ComposerActionItem[] {
  const out: ComposerActionItem[] = []
  for (const app of apps) {
    if (!app || typeof app.name !== 'string' || !app.name) continue
    if (app.enabled === false) continue
    const actions = app.composerActionGrant?.actions
    if (!Array.isArray(actions)) continue
    const label = (typeof app.displayName === 'string' && app.displayName) || (typeof app.manifest?.displayName === 'string' && app.manifest.displayName) || app.name
    for (const raw of actions.slice(0, MAX_COMPOSER_ACTIONS_PER_APP)) {
      if (!raw || typeof raw !== 'object') continue
      const a = raw as Record<string, unknown>
      const id = typeof a.id === 'string' ? a.id : ''
      const title = typeof a.title === 'string' ? a.title : ''
      if (!ID_RE.test(id) || !title || title.length > MAX_COMPOSER_ACTION_TITLE) continue
      const popover = (a.popover && typeof a.popover === 'object' ? a.popover : {}) as Record<string, unknown>
      out.push({
        key: `${app.name}:${id}`,
        appName: app.name,
        appLabel: String(label).slice(0, 80),
        id,
        title,
        icon: typeof a.icon === 'string' && ICON_RE.test(a.icon) ? a.icon : '',
        width: clamp(popover.width, POPOVER_WIDTH),
        maxHeight: clamp(popover.maxHeight, POPOVER_HEIGHT),
      })
    }
  }
  return out
}
