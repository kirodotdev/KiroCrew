/**
 * App-contributed message actions (`contributes.messageActions`): discovery, and the
 * store every chat message reads them from.
 *
 * The manifest is validated server-side, and the gateway's `messageActionGrant`
 * row says what the owner approved: which roles each action is offered on and
 * whether it gets markdown. This module re-checks the shape and the caps, because
 * the list it reads may be stale or hand-edited, and it never offers an action the
 * row does not list.
 *
 * One store, filled by `MessageActionLayer` (mounted once), so a transcript of a
 * thousand messages reads one snapshot instead of running a query each.
 *
 * @module messageActions/messageActionRegistry
 */
import { appDisplayName } from '../components/appstore/appManifest'
import { i18nT } from '../i18n/t'

/** Mirrors the caps in `apps/manifest_contributes.py`. */
export const MAX_MESSAGE_ACTIONS_PER_APP = 2
export const MAX_MESSAGE_ACTION_CHARS = 32000
const MAX_TITLE = 40
const ID_RE = /^[a-z][a-z0-9_-]{0,63}$/
const WIDTH = { min: 200, max: 480, default: 360 }
const HEIGHT = { min: 120, max: 600, default: 480 }

export type MessageRole = 'assistant' | 'user'

export interface ResolvedMessageAction {
  /** `${appName}:${id}`. */
  key: string
  appName: string
  /** The app's name as the user knows it, shown with every item. */
  appLabel: string
  id: string
  title: string
  roles: readonly MessageRole[]
  format: 'plain' | 'markdown'
  maxChars: number
  width: number
  maxHeight: number
  /** Which install of the module this is; a popover made for another revision closes. */
  revision: string
}

interface AppLike {
  name?: string
  displayName?: string
  enabled?: boolean
  version?: string
  installedAt?: string
  updatedAt?: string
  messageActionGrant?: { actions?: unknown }
  manifest?: { contributes?: { messageActions?: unknown } }
}

const clampInt = (n: unknown, lo: number, hi: number, fallback: number): number =>
  typeof n === 'number' && Number.isInteger(n) ? Math.max(lo, Math.min(hi, n)) : fallback

/** Every enabled app's approved actions, ordered by app name. */
export function resolveMessageActions(apps: readonly AppLike[]): ResolvedMessageAction[] {
  const out: ResolvedMessageAction[] = []
  for (const app of apps) {
    if (!app || typeof app.name !== 'string' || !app.name) continue
    if (app.enabled === false) continue
    const declared = app.manifest?.contributes?.messageActions
    const approved = app.messageActionGrant?.actions
    if (!Array.isArray(declared) || !Array.isArray(approved)) continue
    let count = 0
    for (const row of approved) {
      if (count >= MAX_MESSAGE_ACTIONS_PER_APP) break
      if (!row || typeof row !== 'object') continue
      const r = row as Record<string, unknown>
      const id = typeof r.id === 'string' ? r.id : ''
      if (!ID_RE.test(id)) continue
      // Approved AND still declared, with an entry the frame can load.
      const decl = declared.find((d) => d && typeof d === 'object' && (d as Record<string, unknown>).id === id) as Record<string, unknown> | undefined
      const entry = typeof decl?.entry === 'string' ? decl.entry : ''
      if (!entry || entry.includes('..') || entry.startsWith('/')) continue
      const title = typeof r.title === 'string' ? r.title : ''
      if (!title || title.length > MAX_TITLE) continue
      const roles = (Array.isArray(r.roles) ? r.roles : []).filter((x): x is MessageRole => x === 'assistant' || x === 'user')
      if (!roles.length) continue
      const popover = r.popover && typeof r.popover === 'object' ? (r.popover as Record<string, unknown>) : {}
      out.push({
        key: `${app.name}:${id}`,
        appName: app.name,
        appLabel: appDisplayName(app),
        id,
        title,
        roles,
        format: r.format === 'markdown' ? 'markdown' : 'plain',
        maxChars: clampInt(r.maxChars, 1, MAX_MESSAGE_ACTION_CHARS, MAX_MESSAGE_ACTION_CHARS),
        width: clampInt(popover.width, WIDTH.min, WIDTH.max, WIDTH.default),
        maxHeight: clampInt(popover.maxHeight, HEIGHT.min, HEIGHT.max, HEIGHT.default),
        revision: JSON.stringify([entry, app.version ?? '', app.installedAt ?? '', app.updatedAt ?? '']),
      })
      count += 1
    }
  }
  return out.sort((a, b) => a.appLabel.localeCompare(b.appLabel, 'en') || a.id.localeCompare(b.id, 'en'))
}

/**
 * One held-back grant entry (`<id>:role:<role>`, `<id>:format:markdown`) in the
 * owner's words, named with the action's title from the manifest.
 */
export function messageActionGrantLabel(entry: string, declared: unknown): string {
  const [id, kind, value] = entry.split(':')
  const decl = Array.isArray(declared)
    ? (declared.find((d) => d && typeof d === 'object' && (d as Record<string, unknown>).id === id) as Record<string, unknown> | undefined)
    : undefined
  const title = typeof decl?.title === 'string' && decl.title ? decl.title : id
  if (kind === 'role' && value === 'user') return i18nT('components.messageActions.grant_role_user', { title })
  if (kind === 'role' && value === 'assistant') return i18nT('components.messageActions.grant_role_assistant', { title })
  if (kind === 'format' && value === 'markdown') return i18nT('components.messageActions.grant_format_markdown', { title })
  return entry
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

let current: readonly ResolvedMessageAction[] = []
const listeners = new Set<() => void>()

export function setMessageActions(next: readonly ResolvedMessageAction[]): void {
  if (JSON.stringify(next) === JSON.stringify(current)) return
  current = next
  for (const l of listeners) l()
}

export function getMessageActions(): readonly ResolvedMessageAction[] {
  return current
}

/** Set while the app list has failed to load: the menus say so and offer a retry. */
let loadRetry: (() => void) | null = null

export function setMessageActionsLoadError(retry: (() => void) | null): void {
  if (retry === loadRetry) return
  loadRetry = retry
  for (const l of listeners) l()
}

export function getMessageActionsLoadError(): (() => void) | null {
  return loadRetry
}

export function subscribeMessageActions(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** What a message hands an action: its source text and whose message it is. */
export interface MessageActionSource {
  /** The message's markdown as shown (the browsed variant, transcript markers removed). */
  markdown: string
  /** The bubble's visible text, read at the click (`readMessageText`); `null`
   *  when the bubble could not be read, which refuses the plain format. */
  text: string | null
  role: MessageRole
}

export interface MessageActionRequest {
  action: ResolvedMessageAction
  source: MessageActionSource
  /** The element the popover is anchored to: the message bubble. */
  anchor: HTMLElement
}

let opener: ((req: MessageActionRequest) => void) | null = null

/** Set by `MessageActionLayer`; a click before it mounted does nothing. */
export function setMessageActionOpener(open: ((req: MessageActionRequest) => void) | null): void {
  opener = open
}

export function openMessageAction(req: MessageActionRequest): void {
  opener?.(req)
}
