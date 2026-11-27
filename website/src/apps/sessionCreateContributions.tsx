/**
 * Sessions New menu rows contributed by installed apps (`contributes.sessionCreateItems`).
 *
 * An app that must prepare a project before a session exists declares a row; the host
 * renders it, and picking it POSTs to the app's own endpoint, which answers with the
 * prepared project directory. The caller then creates the session through `createSlot`
 * with that project, which binds it before the session is published and deletes the
 * session when the bind fails. Nothing here runs app code.
 *
 * Every rule re-validates what `apps/manifest_session_create.py` checks at install, for
 * the reason `fileMenuContributions.tsx` gives: a manifest can reach the dashboard
 * without passing that check. A bad row is skipped with a warning, never thrown.
 */
import { useMemo } from 'react'
import { useQuery, type QueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { endpointAllowed, FileMenuItemIcon } from './fileMenuContributions'

/** The subset of `GET /api/apps` this module reads. */
export interface SessionCreateAppRecord {
  name: string
  enabled?: boolean
  manifest?: { contributes?: { sessionCreateItems?: unknown } }
}

/** A validated row, ready to render. */
export interface SessionCreateItem {
  id: string
  app: string
  label: string
  icon: string
  endpoint: string
}

/** What the app's endpoint prepared for the new session. */
export interface PreparedAppSession {
  project: string
  title?: string
}

/** Mirrors `MAX_SESSION_CREATE_ITEMS_PER_APP` in `apps/manifest_session_create.py`. */
const MAX_ITEMS_PER_APP = 3
/** Mirrors `MAX_SESSION_CREATE_LABEL` there. */
const MAX_LABEL = 120
/** Mirror `MAX_SESSION_CREATE_ID` and `MAX_SESSION_CREATE_ENDPOINT` there. */
const MAX_ID = 64
const MAX_ENDPOINT = 512
/** Mirrors `SESSION_CREATE_ID_RE` there. */
const ID_RE = /^[a-z0-9][a-z0-9-]*$/
/** A path longer than this is not one the bind route would take seriously. */
const MAX_PROJECT_PATH = 4096

function warnContributionSkipped(appName: string, id: unknown, reason: string): void {
  // eslint-disable-next-line no-console -- a refused contribution is invisible otherwise
  console.warn(`[sessionCreateContributions] app ${appName}: skipping row ${String(id)} — ${reason}`)
}

function readItem(appName: string, raw: unknown): SessionCreateItem | null {
  if (typeof raw !== 'object' || raw === null) {
    warnContributionSkipped(appName, raw, 'entry is not an object')
    return null
  }
  const obj = raw as Record<string, unknown>
  const id = typeof obj.id === 'string' ? obj.id : ''
  const label = typeof obj.label === 'string' ? obj.label : ''
  const icon = typeof obj.icon === 'string' ? obj.icon : ''
  const endpoint = typeof obj.endpoint === 'string' ? obj.endpoint : ''
  if (id.length > MAX_ID || !ID_RE.test(id)) { warnContributionSkipped(appName, obj.id, 'id must be a lowercase kebab slug'); return null }
  if (!label.trim() || label.length > MAX_LABEL) { warnContributionSkipped(appName, id, 'bad label'); return null }
  if (icon.length > MAX_LABEL) { warnContributionSkipped(appName, id, 'bad icon'); return null }
  // In-gateway namespace only, the shape the install check enforces.
  if (endpoint.length > MAX_ENDPOINT || !endpointAllowed(appName, endpoint, false)) {
    warnContributionSkipped(appName, id, `endpoint must route under /api/apps/${appName}/`)
    return null
  }
  return { id, app: appName, label, icon, endpoint }
}

/** Every valid row of the ENABLED installed apps. A disabled app contributes nothing. */
export function sessionCreateItems(apps: readonly SessionCreateAppRecord[]): SessionCreateItem[] {
  const out: SessionCreateItem[] = []
  for (const app of Array.isArray(apps) ? apps : []) {
    if (!app?.enabled || !app.name) continue
    const raw = app.manifest?.contributes?.sessionCreateItems
    if (raw === undefined || raw === null) continue
    if (!Array.isArray(raw)) { warnContributionSkipped(app.name, '(all)', 'sessionCreateItems is not an array'); continue }
    const seen = new Set<string>()
    // Sliced first so the cap bounds the work done while the menu draws.
    for (const entry of raw.slice(0, MAX_ITEMS_PER_APP)) {
      const item = readItem(app.name, entry)
      if (!item || seen.has(item.id)) continue
      seen.add(item.id)
      out.push(item)
    }
  }
  return out
}

/** The rows to render, from the shared `['apps']` cache; issues no request of its own. */
export function useSessionCreateItems(): SessionCreateItem[] {
  const { data: apps } = useQuery({ queryKey: ['apps'], queryFn: () => api.listApps(), enabled: false })
  return useMemo(() => sessionCreateItems((apps ?? []) as SessionCreateAppRecord[]), [apps])
}

/** The endpoint's answer, or `null` when it is not a usable project. */
export function readPreparedSession(raw: unknown): PreparedAppSession | null {
  if (typeof raw !== 'object' || raw === null) return null
  const obj = raw as Record<string, unknown>
  const project = typeof obj.project_dir === 'string' ? obj.project_dir.trim() : ''
  // Absolute (POSIX, a drive, or a share) and NUL-free only; the server resolves it
  // and checks the rest (a share it trusts, a real directory, not a sensitive path,
  // not inside the data home).
  if (!project || project.length > MAX_PROJECT_PATH || project.includes('\0')) return null
  if (!project.startsWith('/') && !/^(?:[A-Za-z]:[\\/]|\\\\)/.test(project)) return null
  if (obj.title !== undefined && typeof obj.title !== 'string') return null
  const title = typeof obj.title === 'string' ? obj.title.trim().slice(0, MAX_LABEL) : ''
  return title ? { project, title } : { project }
}

/**
 * Re-read the app list, find the row again, and POST to its endpoint.
 *
 * The fresh read is the dispatch-time check: a row drawn from an older list must not
 * call an app that has since been disabled, removed, or changed the row. Throws on any
 * failure, so the caller creates no session.
 */
export async function prepareAppSession(queryClient: QueryClient, item: SessionCreateItem): Promise<PreparedAppSession> {
  const failed = () => new Error(i18nT('pages.chat.initialPrompt.new_session_failed'))
  const apps = await queryClient.fetchQuery({ queryKey: ['apps'], queryFn: () => api.listApps(), staleTime: 0 })
  const current = sessionCreateItems((apps ?? []) as SessionCreateAppRecord[])
    .find(it => it.app === item.app && it.id === item.id)
  if (!current || current.endpoint !== item.endpoint) throw failed()
  const prepared = readPreparedSession(await api.prepareAppSession(current))
  if (!prepared) throw failed()
  return prepared
}

/** A row's label with its owning app named beside it, so it cannot read as a core row. */
export function SessionCreateItemLabel({ item }: { item: SessionCreateItem }) {
  return (
    <>
      {/* A row without an icon gets the generic one, so it lines up with the host rows. */}
      <span className="mt-[3px] shrink-0"><FileMenuItemIcon name={item.icon || 'Package'} /></span>
      {/* Two lines, like the private-chat rows: the label wraps at full width and
          the owning app sits under it, so neither is cut off in the narrow menu. */}
      <span className="flex min-w-0 flex-col gap-px">
        <span className="whitespace-normal break-words">{item.label}</span>
        {' '}
        <span className="whitespace-normal break-all text-[11px] leading-snug text-muted">
          {i18nT('components.fileMenuContributions.contributed_by', { app: item.app })}
        </span>
      </span>
    </>
  )
}
