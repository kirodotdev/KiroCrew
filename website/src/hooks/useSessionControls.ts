/**
 * useSessionControls — discovers app-contributed session-bar controls.
 *
 * Apps declare these in `app.json` under `contributes.sessionControls[]`. Before this
 * slot existed an app could contribute a sidebar page and nothing else, so a
 * setting scoped to "this chat" had to be configured on a separate page
 * against a session key the app could not even discover.
 *
 * Discovery is deliberately dumb: read the installed-app list once, keep the
 * enabled apps that declare controls, flatten, and bound the result. The
 * manifest is already validated server-side (kebab-case id, required
 * entryPoint, no path traversal, max per app), so this
 * hook re-checks only the invariants it needs in order to render safely — a
 * stale or hand-edited manifest must not be able to inject a bare `undefined`
 * into the composer.
 *
 * @module hooks/useSessionControls
 */
import { useQueries, useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { SESSION_CONTROL_STATUS_PATH_RE } from '../lib/sessionControlStatusPath'

/** Hard cap on controls rendered inline across ALL apps.
 *
 * The backend caps each app at 2; this caps the composer as a whole. The bar
 * competes with the message input for a single row, so N apps must not be able
 * to grow it without bound. Controls past the cap are dropped, not overflowed —
 * an overflow menu is a follow-up, and silently dropping is better than a
 * composer that cannot be typed in.
 */
export const MAX_INLINE_SESSION_CONTROLS = 2

export interface ResolvedSessionControl {
  /** Composite key, unique across apps: `${appName}:${control.id}`. */
  key: string
  appName: string
  appDisplayName: string
  appVersion: string
  /** Per-app control id, kebab-case. */
  id: string
  /** ESM bundle path relative to the app's `ui/` dir. */
  entryPoint: string
  label: string
  icon: string
  /** Permission lists from the app manifest, forwarded to AppApiProvider. */
  allowedApi: string[]
  allowedEvents: string[]
  /**
   * Optional backend route reporting this control's per-session state, relative
   * to the app's own route base. Empty when the app declares none, in which
   * case the chip carries no state — the pre-existing behaviour.
   */
  statusPath: string
  /**
   * Whether the app runs its backend as a separate PROCESS rather than as
   * in-gateway hooks. The two are served at different prefixes, so this decides
   * which one `statusPath` is resolved against — see `api.appSessionStatus`.
   */
  processBacked: boolean
}

/** Chip state an app may report for the active session. */
export type SessionControlState = 'ok' | 'warn' | 'none'

export interface SessionControlStatus {
  state: SessionControlState
  /** Replaces the chip tooltip when present, so the state can explain itself. */
  tooltip: string
}

/**
 * Normalize and validate a declared `statusPath`, failing closed to ''.
 *
 * The allowlist itself is `SESSION_CONTROL_STATUS_PATH_RE` in
 * `lib/sessionControlStatusPath` — the same constant `api/client` re-applies
 * before interpolating the value — so this layer and that one cannot drift into
 * disagreeing about what a manifest may declare. That module explains why the
 * constant lives on its own rather than in either caller.
 *
 * Exported for tests: this is a security boundary, so the refusals are the
 * behaviour worth pinning.
 */
export function safeStatusPath(raw: unknown): string {
  if (typeof raw !== 'string') return ''
  // A `//host/x` prefix is protocol-relative — a cross-origin URL. Refuse it
  // rather than stripping the slashes, which would turn it into a plausible
  // relative route that satisfies the allowlist below. Harmless here, since the
  // result is interpolated after a `/` and stays same-origin, but the backend
  // refuses it outright and the two layers must not disagree about what a
  // manifest may declare.
  if (raw.startsWith('//')) return ''
  const p = raw.replace(/^\/+/, '')
  return SESSION_CONTROL_STATUS_PATH_RE.test(p) ? p : ''
}

/** States this frontend renders. An unknown value is treated as `none`. */
const KNOWN_STATES = new Set<string>(['ok', 'warn', 'none'])

interface AppLike {
  name?: string
  version?: string
  displayName?: string
  enabled?: boolean
  manifest?: {
    version?: string
    displayName?: string
    contributes?: {
      sessionControls?: {
        id?: string
        entryPoint?: string
        label?: string
        icon?: string
        statusPath?: string
      }[]
    }
    permissions?: { api?: string[]; events?: string[] }
    /**
     * `entryPoint` set means the app runs its own backend PROCESS, which the
     * gateway reverse-proxies at a different prefix than in-gateway hook routes.
     */
    backend?: { entryPoint?: string }
  }
}

/**
 * Flatten installed apps into renderable session controls.
 *
 * Exported for tests: the filtering rules are the whole behaviour of this hook,
 * and they are much easier to pin directly than through a fetch mock.
 */
export function resolveSessionControls(apps: AppLike[]): ResolvedSessionControl[] {
  const out: ResolvedSessionControl[] = []
  // `key` must be unique across the whole result: useSessionControlStatuses
  // keys each status probe on it, and ChatInput uses it as a React list key.
  // appName makes it unique BETWEEN apps; nothing here made it unique WITHIN
  // one, so two controls declaring the same id collapsed into colliding keys —
  // deduping the probes into a single query and colliding the list keys. The
  // server rejects duplicate ids at install, but this function's contract is to
  // survive a stale or hand-edited manifest, so it enforces the invariant it
  // depends on rather than assuming it.
  const seen = new Set<string>()
  for (const app of Array.isArray(apps) ? apps : []) {
    if (!app || typeof app.name !== 'string' || !app.name) continue
    // `enabled === false` is explicit; older payloads omit the field entirely
    // and are treated as enabled, matching AppHost's own guard.
    if (app.enabled === false) continue
    const declared = app.manifest?.contributes?.sessionControls
    if (!Array.isArray(declared)) continue
    const perms = app.manifest?.permissions || {}
    for (const ctl of declared) {
      if (!ctl || typeof ctl !== 'object') continue
      const id = typeof ctl.id === 'string' ? ctl.id : ''
      const entryPoint = typeof ctl.entryPoint === 'string' ? ctl.entryPoint : ''
      // Both are required server-side; without them there is nothing to
      // render and nothing to key on.
      if (!id || !entryPoint) continue
      const key = `${app.name}:${id}`
      // First declaration wins, so the order is stable and a duplicate cannot
      // displace the control that was already resolved.
      if (seen.has(key)) continue
      seen.add(key)
      out.push({
        key,
        appName: app.name,
        appDisplayName: app.manifest?.displayName || app.displayName || app.name,
        appVersion: app.manifest?.version || app.version || '',
        id,
        entryPoint,
        label: (typeof ctl.label === 'string' && ctl.label) || id,
        icon: typeof ctl.icon === 'string' ? ctl.icon : '',
        allowedApi: Array.isArray(perms.api) ? perms.api : [],
        allowedEvents: Array.isArray(perms.events) ? perms.events : [],
        statusPath: safeStatusPath(ctl.statusPath),
        processBacked: Boolean(app.manifest?.backend?.entryPoint),
      })
    }
  }
  // Stable order so chips do not reshuffle between loads. Compared as bytes, not
  // with `localeCompare`: these keys are machine identifiers (`<appName>:<id>`,
  // both kebab-case), never shown to the user, so collation must not vary with
  // the UI language — under a locale-aware compare the same two apps could order
  // differently for two users, and the chip order is part of what a screenshot
  // or a test pins.
  out.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))
  return out.slice(0, MAX_INLINE_SESSION_CONTROLS)
}

/** What {@link useSessionControls} returns. */
export interface SessionControlsResult {
  /** Resolved controls. Empty while loading, and empty on failure. */
  controls: ResolvedSessionControl[]
  /**
   * The app-list query's error, or `null`.
   *
   * Returned rather than swallowed so the caller can render it. The controls
   * themselves still fail closed, but "no chips" and "the app list failed" look
   * identical to the user, so silence here is a dead end rather than a graceful
   * degradation.
   */
  error: Error | null
}

/**
 * Fetch and resolve app-contributed session controls.
 *
 * The controls fail closed: the composer must render whether or not the app list
 * is reachable, so an error yields an empty list rather than breaking the chat.
 * The error is returned alongside them so the caller can surface it — see
 * {@link SessionControlsResult.error}.
 */
export function useSessionControls(): SessionControlsResult {
  // React Query, per the package guideline: deduped with any other reader of
  // the app list, cached stale-while-revalidate, and refetched in the
  // background — none of which the previous useState/useEffect pair did.
  // Shares the app list's own ['apps'] key rather than holding a private
  // ['session-controls'] one. That is what makes a chip react to an app being
  // enabled, disabled or trusted: those paths already invalidate ['apps'] (four
  // call sites), and a separate key saw none of them, so chips stayed stale until
  // their own refetch. Resolution moves into `select`, which runs over whatever
  // the shared key holds — necessary because the key keeps ONE queryFn and the
  // last observer to register is the one that fetches (see `useAppsData`).
  const { data, error } = useQuery({
    queryKey: ['apps'],
    // Guard the call itself, not just its rejection. Tests (and any future
    // trimmed api surface) mock `api` partially, and a missing method would
    // throw synchronously — taking the whole composer down with it.
    queryFn: () => (typeof api?.listApps === 'function' ? api.listApps() : []),
    select: (d: unknown): ResolvedSessionControl[] => {
      const apps = Array.isArray(d) ? d : (d as { apps?: unknown[] })?.apps
      return resolveSessionControls((apps as AppLike[]) || [])
    },
  })
  // The controls still fail closed — an error leaves `data` undefined, so the
  // composer renders without chips rather than breaking. But the error is
  // RETURNED rather than dropped: every chip vanishing is indistinguishable
  // from "no app declares a control" unless the failure is surfaced, so the
  // caller owes the user an error surface. `errors-use-error-notice` names a
  // `useQuery` error as an error for exactly this reason.
  return { controls: data ?? [], error: (error as Error | null) ?? null }
}

/**
 * Normalize one status payload into a chip state.
 *
 * Exported for tests: an app is a third party, so every field here is untrusted
 * and the fallbacks are the whole behaviour.
 */
export function normalizeStatus(raw: unknown): SessionControlStatus {
  // `unknown` rather than `any`: this is a third party's payload, so the
  // narrowing below is the contract and the compiler should enforce that every
  // field is checked before use.
  const r = (raw ?? {}) as { state?: unknown; tooltip?: unknown }
  const state =
    typeof r.state === 'string' && KNOWN_STATES.has(r.state) ? (r.state as SessionControlState) : 'none'
  const tooltip = typeof r.tooltip === 'string' ? r.tooltip.slice(0, 200) : ''
  return { state, tooltip }
}

/** What {@link useSessionControlStatuses} returns. */
export interface SessionControlStatusesResult {
  /** Control key → status, absent for controls with no state. */
  statuses: Record<string, SessionControlStatus>
  /**
   * The first failing probe's error, or `null`.
   *
   * One error for the whole group rather than one per chip: the chips share a
   * row with the message input, so a per-chip banner would cost more than it
   * tells the user.
   */
  error: Error | null
}

/**
 * Poll each control's declared status route for the active session.
 *
 * Exists so a chip can carry state before it is opened: a control's module is
 * lazily imported on first click, so without this a per-session setting looks
 * unset until you go looking for it.
 *
 * The statuses fail closed, like {@link useSessionControls}: an unreachable or
 * malformed status leaves the chip stateless rather than breaking the composer.
 * The error is returned alongside them so the caller can surface it. Controls
 * declaring no `statusPath` cost no request, and neither does a chat with no
 * session yet.
 *
 * @param controls Resolved controls to poll
 * @param sessionKey Active session; part of each query key, since state is per session
 * @param folderId Chat folder the chat is filed in, forwarded as a query
 *   param. The same context the control component receives as a prop. An app
 *   holding a per-folder setting cannot answer without it, and a brand-new chat
 *   is exactly the case where it has no record of its own to fall back on.
 * @param folderName Folder's display name, forwarded so a status tooltip can
 *   name the folder rather than echo an opaque id.
 * @returns `{ statuses, error }` — statuses keyed by control key, absent for
 *   controls with no state, plus the first probe error for the caller to render
 *
 * @example
 * const { statuses } = useSessionControlStatuses(controls, activeSlot, folderId, folderName)
 * statuses['my-app:scope']?.state  // 'ok'
 *
 * To re-poll (e.g. after a control closes), invalidate the shared key:
 * queryClient.invalidateQueries({ queryKey: ['session-control-status'] })
 */
export function useSessionControlStatuses(
  controls: ResolvedSessionControl[],
  sessionKey: string,
  folderId: string = '',
  folderName: string = '',
): SessionControlStatusesResult {
  const probes = controls.filter(c => !!c.statusPath)

  // One query per probe. React Query owns the identity (the queryKey), which
  // replaces the hand-rolled `probes` join string, the `alive` flag, and the
  // refreshToken parameter — a close now invalidates
  // ['session-control-status'] instead of bumping a counter.
  const results = useQueries({
    queries: probes.map(c => ({
      // c.key first: it is the only field guaranteed unique across controls.
      // Two controls in one app may legitimately share a statusPath (distinct
      // ids, same route), and without the key their query keys would be
      // identical — React Query would dedupe them into one query whose result
      // names only one control, leaving the sibling chip permanently stateless.
      // The pre-React-Query probe string keyed on c.key for this reason.
      queryKey: ['session-control-status', c.key, c.appName, c.statusPath, sessionKey, folderId, folderName],
      queryFn: async () => {
        if (typeof api?.appSessionStatus !== 'function') return null
        const params: Record<string, string> = { session_key: sessionKey }
        // An app holding a per-folder setting cannot answer without the
        // folder, and a brand-new chat is exactly the case where it has no
        // record of its own to fall back on.
        if (folderId) params.folder_id = folderId
        if (folderId && folderName) params.folder_name = folderName
        const raw = await api.appSessionStatus(c.appName, c.statusPath, params, c.processBacked)
        return { key: c.key, status: normalizeStatus(raw) }
      },
      // No session means nothing to ask about yet.
      enabled: !!sessionKey,
      // Fails closed: a third-party app that is down must not be retried at
      // the composer's expense, and a missing status is a normal state.
      retry: false,
    })),
  })

  const statuses: Record<string, SessionControlStatus> = {}
  for (const r of results) {
    const d = r.data
    // `none` is dropped rather than stored, so the map reads as "has state"
    // and a caller never has to check the value as well as the key.
    if (d && d.status.state !== 'none') statuses[d.key] = d.status
  }
  // One error stands for all of them: the chips are a group, and a per-chip
  // banner would be noise on a surface that competes with the message input for
  // one row. Statuses still fail closed — a chip whose probe failed is simply
  // stateless — but the failure is reported rather than hidden.
  const failed = results.find(r => !!r.error)
  return { statuses, error: (failed?.error as Error | undefined) ?? null }
}
