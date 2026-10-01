/**
 * The App Kit platform: the installed-app lifecycle, registries and the app
 * store, registry installs including the streamed install, registration, an
 * app's declared per-session status route, and app-contributed file-menu
 * actions.
 */

import { relocateRequestUrl } from '../../lib/dashboardRuntime'
import { normalizeInstalledApps, normalizeInstalledApp } from '../../components/appstore/types'
import { i18nT } from '../../i18n/t'
import { SESSION_CONTROL_STATUS_PATH_RE } from '../../lib/sessionControlStatusPath'
import { ApiError, toApiError } from '../apiError'
import type { ClientTransport } from './transport'

/** Final payload resolved by installFromRegistryStream's SSE `done` event. */
export interface InstallStreamResult {
  ok?: boolean
  error?: string
  /**
   * Machine-readable failure code. The registry install path checks the
   * execution gate BEFORE cloning, so a third-party install can be refused with
   * `app_execution_denied` — and because the stream RESOLVES that refusal (SSE
   * `done`) instead of rejecting, the code has to travel on the result for the
   * consent modal to open at all.
   */
  code?: string
  needsClientInstall?: boolean
  clientInstall?: { shell?: string; postInstall?: string }
}

/**
 * One row of `GET /api/apps/registries`, in either the `registries` (operator)
 * or `pinned` (build) list.
 *
 * `name` is the IDENTITY: the index cache path and every installed app's
 * `_registry` tag are keyed by it, so it is what per-registry counts and refresh
 * calls must use. `label` is a display name shown instead of it, and `review`
 * says how thoroughly the listings were reviewed before publication. Both are
 * display-only and neither changes the credential posture — that is `trust`
 * alone. The backend reports both empty on an operator row and drops them from a
 * PUT, because only the build may make either claim.
 */
export type ExternalRegistryRow = {
  name: string
  repo: string
  branch: string
  trust?: string
  label?: string
  review?: string
}

/** The surfaces a contributed file-menu row can appear on. */
export type FileMenuSurface = 'file-overflow' | 'tree-context' | 'folder-row'

/**
 * What core POSTs to a row's endpoint when it is activated.
 *
 * The PATH only — deliberately never file CONTENT. A contributed row is declared in a
 * manifest and needs no permission to exist, so shipping the bytes with the activation
 * would hand any app that declares one the contents of whatever file the reader clicked,
 * with no install-time declaration and no consent step. An app that needs the bytes reads
 * them through a route its own `permissions` cover.
 */
export interface FileMenuContext {
  surface: FileMenuSurface
  path: string
  kind?: 'file' | 'dir'
  root?: string
}

export function createAppsEndpoints({ get, post, put, del, j, sessionKeyHeader: _sk, checkSessionExpired, removeAuthBanner }: ClientTransport) {
  const platform = {
    // --- Apps ---
    // Installed-app payloads are normalized HERE rather than in a queryFn. The
    // registry feed has one consumer, so `AppsPage` can narrow it at its own
    // `useQuery`; `/api/apps` has four (the Apps page, the left rail, the command
    // palette, the migration check), and normalizing per consumer is how the
    // fourth one gets forgotten. This is the boundary all four share.
    listApps: () => fetch(relocateRequestUrl('/api/apps')).then(j).then(normalizeInstalledApps),
    getApp: (name: string) => fetch(relocateRequestUrl('/api/apps/' + encodeURIComponent(name))).then(j).then(normalizeInstalledApp),
    getAppManifest: (name: string) => fetch(relocateRequestUrl('/api/apps/' + encodeURIComponent(name) + '/manifest')).then(j),
    installApp: (source: string) => post('/api/apps/install', { source }).then(j),
    enableApp: (name: string, sessionApprovalConsent = false) => post('/api/apps/' + encodeURIComponent(name) + '/enable', { sessionApprovalConsent }).then(j),
    disableApp: (name: string) => post('/api/apps/' + encodeURIComponent(name) + '/disable').then(j),
    openApp: (name: string) => post('/api/apps/' + encodeURIComponent(name) + '/open').then(j),
    uninstallApp: (name: string, keepData = true, keepDependencies?: boolean, keepSpecific?: string[]) =>
      post('/api/apps/' + encodeURIComponent(name) + '/uninstall', {
        ...(keepData === false ? { purge_data: true } : {}),
        ...(keepDependencies ? { keep_dependencies: true } : {}),
        ...(keepSpecific?.length ? { keep_specific: keepSpecific } : {}),
      }).then(j),
    uninstallPreview: (name: string) =>
      fetch(relocateRequestUrl('/api/apps/' + encodeURIComponent(name) + '/uninstall/preview')).then(j) as Promise<{
        app: string
        resources: { agents: string[]; skills: string[]; crons: string[] }
        dependencies: {
          removable: { id: string; type: string; reason: string }[]
          shared: { id: string; type: string; usedBy: string[]; reason: string }[]
          userInstalled: { id: string; type: string; reason: string }[]
        }
      }>,
    updateApp: (name: string, source?: string) => post('/api/apps/' + encodeURIComponent(name) + '/update', source ? { source } : {}).then(j),
    migrateCleanup: (name: string) => del('/api/apps/' + encodeURIComponent(name) + '/migrate-cleanup').then(j),
    // apps is intentionally `any[]`: each page (AppsPage/MigrationPage/AppDetailPage)
    // narrows it to its own local RegistryApp shape at the call site. Typing it as
    // unknown[] here would break those structural assignments across files.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    listRegistry: () => fetch(relocateRequestUrl('/api/apps/registry')).then(j) as Promise<{ apps: any[]; serverPlatform: { os: string; arch: string }; categoryOrder?: string[]; editorialSections?: unknown[] }>,
    listRegistries: () => fetch(relocateRequestUrl('/api/apps/registries')).then(j) as Promise<{ registries: ExternalRegistryRow[]; pinned?: ExternalRegistryRow[] }>,
    updateRegistries: (registries: { name: string; repo: string; branch: string; trust?: string }[]) => put('/api/apps/registries', { registries }).then(j) as Promise<{ ok: boolean; registries: ExternalRegistryRow[]; newlyTrustedHosts: string[] }>,
    // Drops the server's on-disk caches of the published documents (catalog /
    // category order / editorial) so the NEXT listRegistry() is rebuilt from
    // fresh fetches instead of waiting out TTLs. A POST because it is a state
    // change (cache deletion + outbound fetches) and must sit behind the CSRF
    // boundary a GET query param would bypass. Deliberately NOT under
    // /api/apps/: that namespace grants an app named `registry` implicit
    // path ownership (token_auth._app_owns_path).
    refreshAppStore: () => post('/api/app-store/refresh').then(j) as Promise<{ ok: boolean }>,
    refreshRegistries: (repo?: string) => post('/api/apps/registries/refresh', repo ? { repo } : {}).then(j) as Promise<{ ok: boolean; refreshed: string[]; failed: string[]; results: { name: string; ok: boolean }[]; apps: number; lastSyncedAt: string }>,
    installFromRegistry: (name: string) => post('/api/apps/registry/install', { name }).then(j),
    /**
     * Stream install logs via SSE.  Calls `onLog` for each line and resolves
     * with the final result JSON when the install completes.
     */
    installFromRegistryStream: async (
      name: string,
      onLog: (line: string) => void,
      signal?: AbortSignal,
    ): Promise<InstallStreamResult> => {
      const res = await fetch(relocateRequestUrl('/api/apps/registry/install-stream'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ..._sk },
        body: JSON.stringify({ name }),
        signal,
      })
      if (!res.ok || !res.body) {
        const text = await res.text()
        throw new Error(text || `HTTP ${res.status}`)
      }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buf += decoder.decode(value, { stream: true })
          // Parse SSE frames: "event: <type>\ndata: <payload>\n\n"
          const frames = buf.split('\n\n')
          buf = frames.pop() || ''
          for (const frame of frames) {
            if (!frame.trim()) continue
            let eventType = ''
            const dataLines: string[] = []
            for (const line of frame.split('\n')) {
              if (line.startsWith('event: ')) eventType = line.slice(7)
              else if (line.startsWith('data: ')) dataLines.push(line.slice(6))
              else if (line === 'data:') dataLines.push('')
            }
            const data = dataLines.join('\n')
            if (eventType === 'log') {
              onLog(data)
            } else if (eventType === 'done') {
              try { return JSON.parse(data) } catch { return { ok: false, error: data } }
            }
          }
        }
        return { ok: false, error: i18nT('api.client.stream_ended_without_completion') }
      } finally {
        reader.releaseLock()
      }
    },
    registerApp: (body: object) => post('/api/apps/register', body).then(j),
  }

  const sessionStatus = {
    /**
     * GET an app's declared per-session status route.
     *
     * Routed through `get()` + `j()` rather than a bare `fetch`, so this call
     * carries the X-Session-Key gate and runs `checkSessionExpired` like every
     * other app call. `statusPath` is validated by the caller against the same
     * allowlist the backend applies at install time.
     *
     * `processBacked` selects the prefix, because an app's backend is served at
     * one of TWO places and picking the wrong one is a silent permanent failure
     * rather than a visible error: in-gateway hook routes are registered under
     * `/api/apps/<app>/`, while an app running its own backend PROCESS is
     * reverse-proxied at `/apps/<app>/api/`. Calling the hook prefix for a
     * process-backed app answers 502 ("no reachable backend"), which the chip
     * renders as a permanently stateless control with nothing saying why.
     */
    appSessionStatus: (
      appName: string,
      statusPath: string,
      params: Record<string, string>,
      processBacked = false,
    ) => {
      // Defensive: the boundary is enforced here rather than deferred to every
      // call site. statusPath comes from a third-party app manifest and is
      // interpolated into the path, so a caller that forgets to sanitize it must
      // not be able to reach /api/apps/<app>/../other-app/... . This is the
      // client-side backstop for the same allowlist the backend applies at
      // install time — callers still validate too, this is not the only check.
      if (!SESSION_CONTROL_STATUS_PATH_RE.test(statusPath)) {
        // A machine code, not UI copy: this throw is a programmer-error backstop for
        // a manifest that got past the caller's own check, so it never renders and
        // must not become a translated string. The comment above is the explanation;
        // the code is what a caller can match on.
        throw new ApiError(400, 'invalidAppStatusPath')
      }
      const qs = new URLSearchParams(params).toString()
      // Both prefixes are built HERE from the app name rather than read from the
      // manifest, so the only app-authored value interpolated into the URL is the
      // `statusPath` already validated above.
      const base = processBacked
        ? '/apps/' + encodeURIComponent(appName) + '/api/'
        : '/api/apps/' + encodeURIComponent(appName) + '/'
      return get(base + statusPath + (qs ? '?' + qs : '')).then(j)
    },
  }

  const fileMenu = {
    // Activate a file-menu row an installed app contributed. The declarations ride on
    // `GET /api/apps` (see `fileMenuContributions.ts`) rather than an endpoint of their
    // own, so only the dispatch lives here: core POSTs the file's path to the row's own
    // endpoint and never imports app code.
    //
    // `sessionKey` is the OWNING SLOT (`dashboard:<slot>`), supplied by the shared
    // dispatcher. It rides the header rather than the body because the server's
    // restricted-session gate reads the header, and the body is the app-facing contract
    // documented in the manifest reference.
    //
    // `redirect: 'error'` is what makes the endpoint allowlist mean anything. The URL is
    // the APP's to choose, and it is validated once, before the request; `fetch` follows a
    // 3xx by default, and a 307 preserves the method, the body AND this header, so an
    // approved endpoint answering `307 /api/apps/<victim>/disable` would have the reader's
    // own session disable another app on a row they merely clicked. Refusing to follow
    // keeps the checked url the only url.
    invokeFileMenuItem: async (
      item: { id: string; endpoint: string },
      ctx: FileMenuContext,
      sessionKey?: string,
    ) => {
      const r = await post(item.endpoint, { item_id: item.id, ...ctx }, sessionKey, undefined, 'error')
      checkSessionExpired(r)
      if (r.ok) { removeAuthBanner(); return r.json() }
      throw await toApiError(r)
    },
  }

  return { platform, sessionStatus, fileMenu }
}
