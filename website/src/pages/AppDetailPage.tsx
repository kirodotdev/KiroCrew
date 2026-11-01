/**
 * AppDetailPage — unified detail view for both installed and registry apps.
 *
 * Route: /apps/detail/:name
 * Fetches from both /api/apps/{name} (installed) and /api/apps/registry (browse).
 * Shows full description, features, screenshots, tags, and action buttons.
 */
import { useEffect, useState, useCallback, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useParams, useNavigate, useLocation } from 'react-router-dom'
import {
  ArrowLeft, Download, Check, Loader2, Power, PowerOff,
  Trash2, RefreshCw, Bot, Zap, ArrowUp,
  Clock, X, Monitor, Copy, Terminal,
  Target, Settings2, Star, ShieldAlert, ExternalLink,
} from 'lucide-react'
import { needsDesktopApp } from '../lib/electron'
import { api } from '../api/client'
import { isNotFoundError } from '../api/apiError'
import { PageHeader, Card, CardTitle, Badge, Btn } from '../components/ui'
import { uiLocation } from '../uiLocations/uiLocation'
import SessionApprovalModes from '../components/appstore/SessionApprovalModes'
import AppIcon from '../components/AppIcon'
import TrustAppModal, {
  APP_EXECUTION_DENIED, DESKTOP_BUILD_STEP_UNSUPPORTED, isTrustDeniedError, useTrustGate,
} from '../components/appstore/TrustAppModal'
import { isRegistrySourced, normalizeRegistryApp, type RegistryApp } from '../components/appstore/types'
import AppSource from '../components/appstore/AppSource'
import { consentTierFor } from '../components/appstore/registryTier'
import { recordEvent } from '../rum'
import { useTheme } from '../hooks/useTheme'
import ErrorNotice from '../components/ErrorNotice'
import { useConfirm } from '../components/ConfirmDialog'
import { findReport, recordError } from '../utils/errorReport'
import { copyCode } from '../utils/clipboard'
import AppStoreDialog from '../components/appstore/AppStoreDialog'
import { safeHttpUrl } from '../lib/safeUrl'

import { i18nT } from '../i18n/t'
import type { AppContributor } from '../types'
import {
  appDisplayName, appDescription, appHighlights, appUseCases, appConfiguration,
} from '../components/appstore/appManifest'
import { isBuiltinServerRow, mergeBuiltinRow } from '../components/appstore/mergeBuiltinRow'
import { classifyManifestArt, installedArt, installedArtList, installedArtListAligned, installedIcon } from '../components/appstore/useHeroArt'
import { fmtDateNumeric, fmtCompact, fmtNumber } from '../i18n/format'
import { HeroBanner, ScreenshotGallery } from './AppDetailPageArt'
import type { AppInfo, NormalizedRegistryEntry, RegistryEntry } from './appDetailPageTypes'
// The page's parts live in sibling owners that never import this module:
// `AppDetailPageArt.tsx` (the hero banner and the screenshot gallery, each with
// its local-art second chance) and `appDetailPageTypes.ts` (the page's view
// model). Callers import the two art components from this module.
export { HeroBanner, ScreenshotGallery }

export default function AppDetailPage() {
  const { name } = useParams<{ name: string }>()
  const [app, setApp] = useState<AppInfo | null>(null)
  const { data: registriesData, error: registriesError } = useQuery({
    queryKey: ['registries'],
    queryFn: () => api.listRegistries(),
    enabled: !!app?._registry && app.origin !== 'local',
  })
  const sourceNames = [...(registriesData?.pinned || []), ...(registriesData?.registries || [])]
    .map(r => ({ name: r.name || r.repo, label: r.label || r.name || r.repo, review: r.review, trust: r.trust }))
  const navigate = useNavigate()
  const location = useLocation()
  const { theme: resolvedMode } = useTheme()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  /**
   * Success reflection for an in-place sync. This page otherwise has only an
   * error surface, so a successful ``update`` re-rendered a byte-identical page:
   * re-copying a source directory normally carries the same version, which makes
   * silence indistinguishable from a no-op. The list card states the outcome for
   * the same reason, and both paths this fix wires need to say it.
   */
  const [successMsg, setSuccessMsg] = useState('')
  const reconsentMsg = app?.sessionApprovalConsentPending
    ? i18nT('pages.appDetailPage.session_approval_reconsent_notice', { name: appDisplayName(app) })
    : ''
  // Declared api/events entries outside the approved set (`approvedGrants`) are
  // held back: the app runs without them until the owner approves them here.
  const stagedApi = app?.approvedGrants
    ? (app.manifest?.permissions?.api || []).filter(p => !(app.approvedGrants?.api || []).includes(p))
    : []
  const stagedEvents = app?.approvedGrants
    ? (app.manifest?.permissions?.events || []).filter(e => !(app.approvedGrants?.events || []).includes(e))
    : []
  // Link-preview hosts and text patterns an update widened wait the same way.
  const stagedLinkHosts = app?.linkPreviewGrant?.stagedHosts || []
  const stagedLinkPatterns = app?.linkPreviewGrant?.stagedTextPatterns || []
  const stagedFullUrl = app?.linkPreviewGrant?.stagedFullUrl === true
  const hasStagedGrants = stagedApi.length + stagedEvents.length + stagedLinkHosts.length + stagedLinkPatterns.length > 0 || stagedFullUrl
  // Shown where the notice was once the owner approves, so the moment is confirmed.
  const [grantsApprovedMsg, setGrantsApprovedMsg] = useState('')
  const enableLabel = app?.sessionApprovalConsentPending
    ? i18nT('pages.appDetailPage.enable_and_allow_chat_control')
    : i18nT('pages.appDetailPage.enable')
  const clearError = useCallback(() => {
    setError('')
  }, [])
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const [installLog, setInstallLog] = useState('')
  const [showInstallLog, setShowInstallLog] = useState(false)
  const [installDone, setInstallDone] = useState(false)
  /**
   * Dismiss for the permanent desktop refusal's banner: one event, one control.
   * The refusal renders the banner AND the install-log panel whose header states
   * the same outcome, so clearing the banner alone would leave "Install failed"
   * on screen beside a re-enabled Install -- or, once `error` is empty, flip the
   * panel's header to "Install complete" over a log that ends in a refusal.
   * Dropping the panel with the banner leaves the page as it was before the
   * attempt, which is what a retry starts from.
   */
  const dismissRefusal = useCallback(() => {
    setError('')
    setShowInstallLog(false)
  }, [])
  const installLogRef = useRef<HTMLPreElement>(null)
  const installAbortRef = useRef<AbortController | null>(null)
  /**
   * The last install failure as reported: its message, for the trust retry to
   * throw, and the machine `code` the stream's `done` payload carried beside it.
   *
   * A REF, not state: the retry closure reads it immediately after `runInstall()`
   * returns, and a state update is not visible in that same tick. The message is
   * the string `reportInstallFailure` journaled, so the consent modal's
   * `ErrorNotice` recovers the endpoint and the install log tail from the same
   * key it renders; the code rides on the thrown error so the modal can tell a
   * permanent refusal from a failure worth retrying.
   */
  const installFailureRef = useRef<{ message: string; code: string }>({ message: '', code: '' })
  const [clientInstall, setClientInstall] = useState<{ shell?: string; postInstall?: string } | null>(null)
  const [copied, setCopied] = useState(false)
  const [serverHostname, setServerHostname] = useState('')
  const [showUninstallConfirm, setShowUninstallConfirm] = useState(false)
  const [keepData, setKeepData] = useState(true)

  // Contributors row (Details panel). Resolved GitHub repo URL for the app's
  // source, its top contributors, and the fetch's loading state. The row hides
  // entirely unless a GitHub repo URL resolves and the fetch yields entries.
  const [repoUrl, setRepoUrl] = useState('')
  const [contributors, setContributors] = useState<AppContributor[]>([])
  const [contribLoading, setContribLoading] = useState(false)
  /** A rejected contributors fetch, kept apart from `[]` so a transport failure
   *  does not read as "no contributors". */
  const [contribError, setContribError] = useState('')

  useEffect(() => {
    // Prefer the registry/manifest repo, then the git URL the app was trusted
    // from, then its source. GitHub-only in v1; a non-GitHub value leaves the
    // row hidden (the backend also returns [] for one).
    const candidates = [app?.repo, app?.manifest?.repo, app?.trustRepository, app?.source]
    const url = candidates.find(
      (v): v is string => typeof v === 'string' && /^https:\/\/github\.com\//i.test(v),
    ) || ''
    setRepoUrl(url)
    setContribError('')
    if (!url) { setContributors([]); setContribLoading(false); return }
    let cancelled = false
    setContribLoading(true)
    api.appContributors(url)
      .then(res => { if (!cancelled) setContributors(res.contributors || []) })
      .catch((e: unknown) => { if (!cancelled) { setContributors([]); setContribError(e instanceof Error ? e.message : String(e)) } })
      .finally(() => { if (!cancelled) setContribLoading(false) })
    return () => { cancelled = true }
  }, [app])

  /** Journal an install failure WITH the tail of its log, then show it.
   *
   *  The streaming install bypasses the `api.*` journal hook, so without this the
   *  agent hand-off on the failure notice would carry only the one-line message.
   *  The log is read from the rendered <pre> (the accumulated state, not a stale
   *  closure); `recordError` redacts and caps it. The message string is the
   *  journal key, so it must be exactly what `setError` shows. `code` is the
   *  `done` payload's machine code, when it carried one, journaled so a client
   *  can act on the failure instead of matching its prose. */
  const reportInstallFailure = useCallback((message: string, source: 'api' | 'system', code = '') => {
    recordError({
      source,
      message,
      code: code || undefined,
      endpoint: '/api/apps/registry/install-stream',
      detail: installLogRef.current?.textContent || undefined,
    })
    installFailureRef.current = { message, code }
    setError(message)
  }, [])

  // Abort in-flight streaming install on unmount
  useEffect(() => () => { installAbortRef.current?.abort() }, [])

  const load = useCallback(async () => {
    if (!name) return
    setLoading(true)
    // Drop the previous record before resolving the new name: a genuine miss
    // sets nothing below, and without this an in-session navigation from a
    // loaded app to a non-existent one would keep rendering the old app under
    // the new URL instead of the not-found page.
    setApp(null)
    clearError()
    try {
      // Try installed app first. Only a 404 means "not installed"; any other
      // rejection is a load failure and must not be shown as "not found".
      let installed: Awaited<ReturnType<typeof api.getApp>> | null = null
      try {
        installed = await api.getApp(name)
      } catch (e: unknown) {
        if (!isNotFoundError(e)) throw e
      }
      // Also check registry for richer metadata (screenshots, highlights). A
      // failure here is held rather than swallowed: an INSTALLED app can still
      // render from its manifest, but the reader is told the catalog was not
      // reachable; an app that is not installed cannot be resolved without it.
      //
      // Rows are normalized ONCE here, at the fetch site, by the same
      // `normalizeRegistryApp` the browse list uses -- so every branch below
      // reads coerced display fields and a sanitized star count instead of
      // re-defending them field by field. The RAW rows are kept beside them for
      // one reader only: `mergeBuiltinRow` is row-first, and normalize fills a
      // missing `displayName` with the slug, which would then beat the
      // manifest's own name. The merge already accepts a loose row and fills
      // its own gaps, so it is handed the row as the server sent it.
      let rawRegistryList: RegistryEntry[] = []
      let registryList: NormalizedRegistryEntry[] = []
      let sideFailure: unknown = null
      try {
        const registryData = await api.listRegistry()
        rawRegistryList = (registryData.apps || []) as RegistryEntry[]
        registryList = rawRegistryList.map(
          (r) => normalizeRegistryApp(r as RegistryApp) as NormalizedRegistryEntry,
        )
      } catch (e: unknown) {
        sideFailure = e
      }

      // Fetch server hostname for client install template variables
      try {
        const sysInfo = await api.system()
        if (sysInfo.hostname) setServerHostname(sysInfo.hostname)
      } catch (e: unknown) {
        sideFailure ??= e
      }
      const registryEntry = registryList.find((r) => r.name === name)
      const rawRegistryEntry = rawRegistryList.find((r) => r.name === name)

      if (installed) {
        const m = installed.manifest || {}
        // A built-in the registry response carries goes through the SAME merge
        // the browse list uses. Spelled separately, the two chains disagreed:
        // this page preferred the manifest (`m.author || registryEntry?.author`)
        // while the list preferred the row, so one app could read
        // "Kiro Crew · Developer Tools" in the list and "kirocrew · Productivity"
        // one click later. The catalog is the store's inventory on both surfaces
        // or on neither.
        if (rawRegistryEntry && registryEntry && isBuiltinServerRow(registryEntry)) {
          setApp({
            ...mergeBuiltinRow(rawRegistryEntry, { ...m, version: installed.version }),
            name: installed.name,
            installed: true,
            installedVersion: installed.version,
            enabled: installed.enabled,
            sessionApprovalConsentPending: installed.sessionApprovalConsentPending,
            approvedGrants: installed.approvedGrants,
            linkPreviewGrant: installed.linkPreviewGrant,
            managed: installed.managed,
            source: installed.source,
            installedAt: installed.installedAt,
            origin: installed.origin,
            resources: installed.resources,
            lifecycle: installed.lifecycle,
            updateAvailable: registryEntry.updateAvailable || false,
            // Built-ins never carry a star count (they have no repository of
            // their own) — enforce the invariant here rather than trusting the
            // spread above, since mergeBuiltinRow copies the raw server row.
            stargazersCount: undefined,
            manifest: m,
          })
        } else {
          // A non-built-in installed app may have no registry row carrying art
          // at all — a local-directory install has none, and a row built from a
          // cached manifest older than the release that added the art carries
          // those fields empty. The manifest on disk still has the paths, and
          // since the app IS installed those paths resolve against its own
          // install directory through `installedArt` — no repo identifier, no
          // clone, no network.
          // A page's own icon ships inside the app's UI bundle, not at the repo
          // root, so a relative value resolves against the app's UI asset route —
          // the same base the rail and the command palette use. A cross-origin
          // value is refused here for the same reason it is everywhere else on
          // this path: the manifest is untrusted, and requesting it would leak the
          // viewer to whatever host it names.
          const pageIcon: unknown = m.ui?.pages?.[0]?.iconUrl
          const pageIconKind = classifyManifestArt(pageIcon)
          const pageIconUrl = pageIconKind === 'same-origin' ? pageIcon as string
            : pageIconKind === 'relative' ? `/apps/${installed.name}/ui/${pageIcon as string}`
              : ''
          setApp({
            name: installed.name,
            displayName: installed.displayName || m.displayName || installed.name,
            description: m.description || '',
            // The installed record wins, and the registry row is the LAST
            // resort. Version is the one field where the catalog does not get
            // to speak for the machine: the row is fetched from the network and
            // cached, so it can name an older version than the clone installed
            // here — a repo publishing 0.1.0 while this machine runs 0.2.0. The
            // built-in branch above resolves it the same way
            // (`mergeBuiltinRow(registryEntry, { ...m, version: installed.version })`,
            // whose contract names version as its one reversed field), so both
            // branches agree rather than disagreeing the way the comment there
            // records for `author`.
            version: installed.version || m.version || registryEntry?.version || '0.0.0',
            author: m.author || registryEntry?.author || '',
            _registry: registryEntry?._registry,
            provenance: registryEntry?.provenance,
            catalogListed: !!registryEntry,
            icon: registryEntry?.icon || m.ui?.pages?.[0]?.icon || '',
            // `iconPath` is preferred over a manifest-declared `iconUrl` for the
            // same reason the backend honours only `iconPath`: a repo-relative
            // path stays on our own proxy, which enforces the extension
            // allowlist and the trusted-repo gate. The `iconUrl` fallback goes
            // through the same resolver rather than straight to `<img>`, so a
            // manifest naming an external host is refused on this surface too.
            iconUrl: registryEntry?.iconUrl
              || installedIcon(m.iconPath, m.iconUrl, installed.name) || pageIconUrl || '',
            iconUrlDark: registryEntry?.iconUrlDark
              || installedIcon(m.iconPathDark, m.iconUrlDark, installed.name) || '',
            // The app IS installed on this branch, so its icon bytes are on
            // local disk: carry that route as a LOAD-failure fallback for
            // AppIcon. Deliberately not a precedence change — the registry's
            // immutable content-addressed asset above stays the primary `src`
            // and keeps its cache-forever win (#6804 rejects a flip); the local
            // candidate is consulted only when that src errors (offline,
            // captive portal, blocked host). When the local candidate itself
            // won the precedence above, AppIcon skips the identical URL.
            iconUrlFallback: installedIcon(m.iconPath, m.iconUrl, installed.name) || '',
            iconUrlFallbackDark: installedIcon(m.iconPathDark, m.iconUrlDark, installed.name) || '',
            tags: m.tags || registryEntry?.tags || [],
            highlights: m.highlights || registryEntry?.highlights || [],
            useCases: m.useCases || registryEntry?.useCases || [],
            configuration: m.configuration || registryEntry?.configuration || [],
            // `||` would be wrong for the list fields: an empty array is truthy,
            // so a declared-but-unresolvable list would short-circuit the
            // blob-proxy fallback instead of falling through to it.
            screenshots: registryEntry?.screenshots
              || installedArtList(m.screenshots, installed.name),
            screenshotsDark: registryEntry?.screenshotsDark
              || installedArtList(m.screenshotsDark, installed.name),
            heroImage: registryEntry?.heroImage || installedArt(m.heroImage, installed.name),
            heroImageDark: registryEntry?.heroImageDark
              || installedArt(m.heroImageDark, installed.name),
            heroImageDetail: registryEntry?.heroImageDetail
              || installedArt(m.heroImageDetail, installed.name),
            heroImageDetailDark: registryEntry?.heroImageDetailDark
              || installedArt(m.heroImageDetailDark, installed.name),
            // The app IS installed on this branch, so its hero/screenshot
            // bytes are on local disk: carry those routes as LOAD-failure
            // fallbacks, the same shape as the icon pair above. Deliberately
            // not a precedence change — the registry assets stay the primary
            // `src` (#6804 rejects a flip); these are consulted only when a
            // primary errors (offline, captive portal, blocked host) (#6864).
            heroImageFallback: installedArt(m.heroImage, installed.name),
            heroImageDarkFallback: installedArt(m.heroImageDark, installed.name),
            heroImageDetailFallback: installedArt(m.heroImageDetail, installed.name),
            heroImageDetailDarkFallback: installedArt(m.heroImageDetailDark, installed.name),
            // The screenshot fallbacks pair with their primaries BY INDEX, so
            // they use the aligned resolver (refused entries stay as ''
            // placeholders) — the filtered list would shift every entry after
            // a refusal and pair a thumbnail with its neighbour's art. Set
            // only when the registry supplied the primary list: when the
            // local list won the precedence above, the primary already IS the
            // local route (filtered, so aligned indices would not match), and
            // retrying an identical URL is a guaranteed second failure.
            screenshotsFallback: registryEntry?.screenshots
              ? installedArtListAligned(m.screenshots, installed.name)
              : undefined,
            screenshotsDarkFallback: registryEntry?.screenshotsDark
              ? installedArtListAligned(m.screenshotsDark, installed.name)
              : undefined,
            // Left as the row's own value: this field also names the repo in the
            // trust-consent prompt and the details list, and widening those to a
            // fallback identifier is a separate decision from resolving art.
            repo: registryEntry?.repo || '',
            trustRepository: installed.trustRepository,
            stargazersCount: registryEntry?.stargazersCount,
            installed: true,
            installedVersion: installed.version,
            enabled: installed.enabled,
            sessionApprovalConsentPending: installed.sessionApprovalConsentPending,
            approvedGrants: installed.approvedGrants,
            linkPreviewGrant: installed.linkPreviewGrant,
            managed: installed.managed,
            source: installed.source,
            installedAt: installed.installedAt,
            origin: installed.origin,
            resources: installed.resources,
            lifecycle: installed.lifecycle,
            updateAvailable: registryEntry?.updateAvailable || false,
            manifest: m,
          })
        }
      } else if (registryEntry) {
        setApp({
          // Already normalized at the fetch site: display fields are strings
          // with their fallbacks filled and the star count is sanitized.
          ...registryEntry,
          // Preserve install status from registry (set by detectInstalled)
          installed: registryEntry.installed ?? false,
          platform: registryEntry.platform,
        })
      } else if (sideFailure) {
        // Neither installed nor in the catalog — but the catalog read FAILED, so
        // this is a load failure, not a missing app.
        throw sideFailure
      }
      // Otherwise a real not-found: `app` stays null with no error, and the
      // render below shows the "doesn't exist" page for exactly that pair.
      if (sideFailure) setError(sideFailure instanceof Error ? sideFailure.message : String(sideFailure))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : i18nT('pages.appDetailPage.failed_to_load_app'))
    } finally {
      setLoading(false)
    }
  }, [name, clearError])

  useEffect(() => { load() }, [load])

  // Auto-trigger install/update after an IN-APP action navigated here.
  //
  // The trigger is router state (``location.state.autoAction``) and ONLY that:
  // a query param is attacker-reachable — a cross-site page can navigate an
  // authenticated browser to a detail URL and the Lax session cookie rides
  // along, so any URL-driven trigger (install OR update, since update installs
  // an absent app) would run third-party setup code with gateway privileges
  // without user intent. Router state can only be produced by an in-app
  // navigate() call, so it cannot be forged from outside the app.
  const autoActionTriggered = useRef(false)
  useEffect(() => {
    if (!app || autoActionTriggered.current) return
    const stateAction = (location.state as { autoAction?: string } | null)?.autoAction
    if (stateAction !== 'install' && stateAction !== 'update') return
    if (stateAction === 'install' && app.installed) return
    autoActionTriggered.current = true
    // Clear the state so a refresh or Back/Forward doesn't re-fire it.
    navigate(`${location.pathname}${location.search}`, { replace: true, state: null })
    // An installed app whose bytes came from a directory on this machine has no
    // registry row to install from — its refresh is the update endpoint, which
    // re-copies the source directory recorded at install. The streaming registry
    // install is for everything else: a registry-sourced app, and an app not
    // installed at all.
    if (stateAction === 'update' && app.installed && !isRegistrySourced(app)) {
      handleAction('update')
      return
    }
    handleInstall()
  }, [app, location]) // eslint-disable-line react-hooks/exhaustive-deps

  /**
   * The registry install itself. Resolves `'trust-required'` when the gateway
   * refused it for missing execution trust instead of failing it, so the CALLER
   * owns the consent modal — which is what lets this same function BE the retry
   * the modal re-runs once the grant lands.
   *
   * `'failed'` is reported separately from `'done'` for every unsuccessful
   * install. The distinction is load-bearing rather than cosmetic: when this
   * function runs AS the trust retry, the modal rolls its fresh grant back only if
   * the retry rejects, and collapsing an ordinary `{ok:false}` failure into
   * `'done'` therefore left a grant standing over a name no app occupies — a
   * consent bypass for whatever gets installed under that name next.
   */
  const runInstall = async (): Promise<'done' | 'trust-required' | 'failed' | 'aborted'> => {
    if (!app) return 'done'
    setActionLoading('install')
    setInstallLog('')
    setInstallDone(false)
    setShowInstallLog(true)
    clearError()
    // Drop the previous attempt's reason: a retry that fails for a new cause (or
    // is aborted, which has none) must never show the first attempt's.
    installFailureRef.current = { message: '', code: '' }
    setClientInstall(null)
    installAbortRef.current?.abort()
    const controller = new AbortController()
    installAbortRef.current = controller
    try {
      const result = await api.installFromRegistryStream(
        app.name,
        (line) => {
          setInstallLog(prev => prev + (prev ? '\n' : '') + line)
          // Auto-scroll to bottom
          requestAnimationFrame(() => {
            if (installLogRef.current) {
              installLogRef.current.scrollTop = installLogRef.current.scrollHeight
            }
          })
        },
        controller.signal,
      )
      // Server says this app needs client-side installation
      //
      // Deliberately `'done'` and NOT a non-terminal outcome, unlike the abort and
      // failure paths below. Nothing is on disk yet, so the rollback probe would
      // 404 and withdraw the grant — but the grant is exactly what the user just
      // consented to so they can complete the client-side install being shown to
      // them. Withdrawing it here would break the flow it was granted for. The
      // residual window (user consents, then abandons the client install, leaving
      // a grant with no owner) is real but is a product question about that flow,
      // not a defect in this one: revoking is available in Settings, and the
      // uninstall path refuses to leave the grant behind if they finish later.
      if (result.needsClientInstall) {
        setClientInstall(result.clientInstall || app.platform?.clientInstall || {})
        setShowInstallLog(false)
        setActionLoading(null)
        return 'done'
      }
      // A refused install is a consent prompt, not an error. The gate runs
      // BEFORE the clone, so nothing landed on disk and the log holds nothing
      // the user needs to read — drop the log panel and hand the refusal back.
      // The stream RESOLVES this refusal (SSE `done` carries the code), so it is
      // checked on the result, not only in the catch below.
      if (isTrustDeniedError(result)) {
        setShowInstallLog(false)
        return 'trust-required'
      }
      setInstallDone(true)
      if (result.ok) {
        recordEvent('app_install', { app: app.name, source: 'registry', version: app.version })
        await load()
        window.dispatchEvent(new Event('mc:apps-changed'))
      } else {
        reportInstallFailure(result.error || i18nT('pages.appDetailPage.install_failed'), 'system', result.code || '')
        return 'failed'
      }
    } catch (e: unknown) {
      // An ABORT is not a completed install, and reporting it as `'done'` was the
      // third way a grant could be orphaned (after an ordinary `{ok:false}` and
      // after a second trust refusal). Navigating away aborts the stream, so:
      // confirm trust -> navigate -> the install never lands -> the retry resolved
      // -> nothing rejected -> the fresh grant stayed over a name no app occupies.
      // Reported as its own outcome so the retry rejects and the rollback probe
      // decides from what is ACTUALLY installed: if the abort raced a completed
      // install the app is there and the grant is rightly kept, and if it did not
      // land the 404 withdraws it.
      if (e instanceof Error && e.name === 'AbortError') return 'aborted'
      // The non-streaming install route answers 403 with the same code.
      if (isTrustDeniedError(e)) {
        setShowInstallLog(false)
        return 'trust-required'
      }
      setInstallDone(true)
      reportInstallFailure(e instanceof Error ? e.message : i18nT('pages.appDetailPage.install_failed'), 'api')
      return 'failed'
    } finally {
      // Only clear loading if this is still the active install —
      // compare by identity to avoid the race where a second invocation
      // replaces the ref before this finally runs.
      if (installAbortRef.current === controller) {
        setActionLoading(null)
      }
    }
    return 'done'
  }

  /** The single enable path — shared by the action buttons and the trust retry. */
  const runEnable = useCallback(async (name: string) => {
    await api.enableApp(name, app?.sessionApprovalConsentPending === true)
    recordEvent('app_enable', { app: name, version: app?.installedVersion || app?.version })
    await load()
    window.dispatchEvent(new Event('mc:apps-changed'))
  }, [app, load])

  const trust = useTrustGate(runEnable)
  // The listing registry's review tier, resolved at RENDER time rather than
  // when the gate opens: the Get button's autoAction install can be refused
  // before the registries list lands, and a tier snapshotted then would be
  // missing for the whole dialog. A local app has no listing, so it carries no
  // claim, the same rule the Source line applies. Display only. The app's
  // `_registry` rides on the target too, as it does on Discover and Library, so
  // the dialog's source badge names the listing registry (whatever its tier)
  // instead of the first-party fallback label (#20656).
  // Until the registries list lands (or when it fails) the tier is unknown, so
  // the not-vetted box shows (`consentTierFor`) rather than read it as vetted.
  const consentTarget = trust.target && app && app.name === trust.target.name && app.origin !== 'local' && app._registry
    ? { ...trust.target, _registry: app._registry, ...consentTierFor(app._registry, sourceNames, registriesData === undefined) }
    : trust.target
  const { confirm: confirmConsent, confirmDialog: consentDialog } = useConfirm()

  /**
   * Get / Install / Update entry point — owns the consent modal.
   *
   * Every install surface funnels here (the Get button, the two Update buttons,
   * and the `autoAction` navigation the App Store's Get uses), so the refusal is
   * handled once no matter which one triggered it. The retry re-runs the INSTALL,
   * not the enable: this refusal came from the registry-install gate and there is
   * nothing installed yet to enable.
   */
  const handleInstall = async () => {
    if (!app) return
    if (await runInstall() !== 'trust-required') return
    trust.open(
      {
        name: app.name,
        displayName: app.displayName,
        trustRepository: app.trustRepository,
        origin: app.origin,
        sessionApproval: app.manifest?.permissions?.sessionApproval === true,
        retryInstalls: true,
        // The Update buttons funnel here too, over an app that is already there.
        replacesInstalled: app.installed === true,
      },
      async () => {
        // ANY unsuccessful retry must REJECT, not resolve. `useTrustGate` rolls the
        // fresh grant back on rejection (and only then), so resolving here on an
        // ordinary install failure left the grant orphaned over a name no app
        // occupies. A second trust refusal additionally means the grant did not
        // take effect, which the modal reports inline rather than closing on a
        // silent no-op.
        const outcome = await runInstall()
        // Carries the code as a property so `isTrustDeniedError` reads it as the
        // trust refusal it is, the same as a refused enable's response body.
        if (outcome === 'trust-required') {
          throw Object.assign(new Error(APP_EXECUTION_DENIED), { code: APP_EXECUTION_DENIED })
        }
        // The server's own sentence, so the modal can show WHY instead of only
        // the generic copy, with the machine code beside it so the modal can tell
        // a permanent refusal from a failure worth retrying (`errorCode` reads a
        // direct `code` property). `installFailureRef` holds what
        // `reportInstallFailure` journaled; an abort reports nothing, so it falls
        // back to the generic string rather than reusing a stale reason.
        if (outcome !== 'done') {
          const { message, code } = installFailureRef.current
          throw Object.assign(
            new Error(message || i18nT('pages.appDetailPage.install_failed')),
            code ? { code } : {},
          )
        }
      },
    )
  }

  const handleAction = async (action: 'enable' | 'disable' | 'uninstall' | 'update') => {
    if (!app) return
    // Intercept uninstall to show confirmation modal
    if (action === 'uninstall') {
      setShowUninstallConfirm(true)
      setKeepData(true)
      return
    }
    setActionLoading(action)
    let updateResult: { notice?: string } | undefined
    clearError()
    setSuccessMsg('')
    try {
      if (action === 'enable') {
        // Consent to chat control is restated at click time, on the trusted and
        // the untrusted path alike, so the button has one ceremony: a dialog
        // that names the grant, then enable (the trust modal may still follow
        // for an untrusted repository).
        if (app.sessionApprovalConsentPending) {
          const ok = await confirmConsent({
            title: i18nT('pages.appDetailPage.session_approval_confirm_title', { name: appDisplayName(app) }),
            body: i18nT('pages.appDetailPage.session_approval_desc'),
            confirmLabel: i18nT('pages.appDetailPage.enable_and_allow_chat_control'),
            danger: false,
          })
          if (!ok) return
        }
        await runEnable(app.name)
        await load()
        return
      }
      if (action === 'disable') await api.disableApp(app.name)
      else if (action === 'update') updateResult = await api.updateApp(app.name)
      if (action === 'disable') {
        recordEvent('app_disable', { app: app.name, version: app.installedVersion || app.version })
      }
      await load()
      // `load()` clears the error but does not speak to success, and a same-version
      // re-copy changes nothing visible, so say it explicitly. The Sync button that
      // reaches here is not gated on source, and `handle_update_app` re-clones a
      // registry-sourced app from the registry rather than copying a directory, so
      // each case has to name where the update actually came from.
      if (action === 'update') {
        // A widened session-approval grant leaves the app DISABLED pending
        // re-consent (`notice: session_approval_reconsent`). Saying "updated"
        // alone would report success over an app that just stopped running, so
        // that case gets its own persistent notice instead of the 4s toast.
        if (updateResult?.notice !== 'session_approval_reconsent') {
          setSuccessMsg(isRegistrySourced(app)
            ? i18nT('pages.appsPage.updated_from_the_registry', { name: appDisplayName(app) })
            : i18nT('pages.appsPage.synced_from_its_source_directory', { name: appDisplayName(app) }))
          setTimeout(() => setSuccessMsg(''), 4000)
        }
      }
      window.dispatchEvent(new Event('mc:apps-changed'))
    } catch (e: unknown) {
      // A third-party app that has not been granted execution trust yet is a
      // consent prompt, not an error — branch on the machine-readable code, and
      // let the modal grant it inline instead of sending the user to a blanket
      // switch. Every OTHER failure still renders its own prose.
      if (action === 'enable' && isTrustDeniedError(e)) {
        trust.open({
          name: app.name,
          displayName: app.displayName,
          trustRepository: app.trustRepository,
          origin: app.origin,
          sessionApproval: app.manifest?.permissions?.sessionApproval === true,
        })
      } else {
        setError(e instanceof Error ? e.message : i18nT('pages.appDetailPage.failed_to', { action }))
      }
    } finally {
      setActionLoading(null)
    }
  }

  const confirmUninstall = async () => {
    if (!app) return
    setActionLoading('uninstall')
    clearError()
    try {
      const res = await api.uninstallApp(app.name, keepData)
      if (res.uninstall_log) setInstallLog(res.uninstall_log)
      recordEvent('app_uninstall', { app: app.name, version: app.installedVersion || app.version })
      window.dispatchEvent(new Event('mc:apps-changed'))
      navigate('/apps')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : i18nT('pages.appDetailPage.failed_to_uninstall'))
    } finally {
      setActionLoading(null)
      setShowUninstallConfirm(false)
    }
  }

  if (loading) {
    return (
      <>
        <PageHeader title={i18nT('pages.appDetailPage.apps')} subtitle={i18nT('pages.appDetailPage.loading')} />
        <div className="flex-1 flex items-center justify-center text-muted text-sm">
          <Loader2 size={16} className="animate-spin mr-2" /> {i18nT('pages.appDetailPage.loading_app_details')}
        </div>
      </>
    )
  }

  if (!app) {
    // Two different pages share this branch: `error` set means the load FAILED
    // (transport, 5xx), so the header must not announce "not found" and the
    // failure gets the shared surface plus a retry; no error means the name
    // genuinely resolves to nothing.
    return (
      <>
        <PageHeader
          title={error ? i18nT('pages.appDetailPage.apps') : i18nT('pages.appDetailPage.app_not_found')}
          subtitle={error ? i18nT('pages.appDetailPage.failed_to_load_app') : i18nT('pages.appDetailPage.doesnt_exist', { name })}
        />
        <div className="flex-1 flex flex-col items-center justify-center gap-4 p-8">
          {/* Nothing on this page is editable, so the hand-off loses nothing. */}
          <ErrorNotice message={error} askAgent />
          <div className="flex items-center gap-2">
            {error && <Btn onClick={() => { void load() }}>{i18nT('pages.appDetailPage.retry')}</Btn>}
            <Btn onClick={() => navigate('/apps')}><ArrowLeft size={14} /> {i18nT('pages.appDetailPage.back_to_apps')}</Btn>
          </div>
        </div>
      </>
    )
  }

  const isSelfManaged = app.resources === 'app'
  const isBuiltin = app.origin === 'builtin'
  // An app can declare that its UI only works inside the Electron shell
  // (native always-on-top windows, global shortcuts, tray). Browser sessions
  // are told to use the desktop app instead of being handed a broken UI.
  // UX gate only: the marker is client-side, so nothing security-relevant may
  // rest on it (see PlatformConfig.requiresDesktopApp in manifest.py).
  const desktopOnly = needsDesktopApp(app)
  const canUpdate = app.lifecycle === 'gateway'
  const canUninstall = app.lifecycle !== 'locked'
  // Resource lists, derived once. `manifest` is absent for a registry-only app,
  // and `normalizeInstalledApp` fills the lists for an installed one — so the
  // fallback here is the registry case, not a defence against a partial
  // manifest, and nothing below re-asserts past it (#3689).
  const agents = app.manifest?.agents || []
  const skills = app.manifest?.skills || []
  const crons = app.manifest?.crons || []
  // Source-repo and issue-tracker links for the Details card (#9912). The repo
  // Source-repo and issue-tracker links for the Details card (#9912). The repo
  // string is third-party manifest/registry input, so it becomes a link ONLY
  // when it parses as an http(s) URL with no userinfo (safeHttpUrl rejects
  // javascript:/data: and credential-smuggling forms); anything else falls back
  // to plain text. `app.repo` is the value the card already DISPLAYS, so the
  // link's text and its href are built from the SAME string — otherwise the
  // text could name one repo while the click opened another (`repoUrl` may be
  // resolved from manifest.repo/trustRepository/source, not app.repo).
  // `typeof` guard first: `app.repo` is third-party registry data typed as a
  // string, but an entry can ship `"repo": ["…"]`; safeHttpUrl would URL-coerce
  // the array to validate yet return the array itself, and `.replace` on it
  // would throw and blank the whole detail page. A non-string repo stays plain
  // text, exactly as it rendered before this change.
  const sourceRepoUrl = typeof app.repo === 'string' && app.repo ? safeHttpUrl(app.repo) : null
  const sourceRepoHost = (() => {
    try { return sourceRepoUrl ? new URL(sourceRepoUrl).hostname : '' } catch { return '' }
  })()
  // Issue tracker: a GitHub repo's /issues page, built from the same displayed
  // repo so the link cannot point somewhere the row does not name. Scoped to
  // GitHub (the only host whose tracker path this knows) and deliberately a
  // plain outbound LINK, never an in-app flow that files on the user's behalf —
  // that needs new manifest metadata and a credential/product decision the
  // maintainer owns (#9912).
  const issuesUrl =
    sourceRepoUrl && sourceRepoHost === 'github.com'
      ? `${sourceRepoUrl.replace(/\/+$/, '').replace(/\.git$/, '')}/issues`
      : ''
  // Theme-aware hero banner source (mirrors the Browse card resolution).
  // Prefer the wide detail-ratio banner (heroImageDetail*); fall back to the
  // Browse hero, then the opposite theme.
  const heroDetailSrc = resolvedMode === 'dark'
    ? (app.heroImageDetailDark || app.heroImageDetail || '')
    : (app.heroImageDetail || app.heroImageDetailDark || '')
  const heroBrowseSrc = resolvedMode === 'dark'
    ? (app.heroImageDark || app.heroImage || '')
    : (app.heroImage || app.heroImageDark || '')
  const heroSrc = heroDetailSrc || heroBrowseSrc
  // When a dedicated detail banner is shown, size the container to its
  // 1200x288 (25:6) ratio so object-cover doesn't horizontally crop the art
  // on viewports narrower than 1200px. Fall back to 16:9 for the Browse hero.
  const heroIsDetail = Boolean(heroDetailSrc)
  // Local-art fallback candidate (#6864): the SAME two-level choice
  // re-evaluated over the fallback fields. The detail-vs-Browse order cannot
  // put detail-ratio art into the 16:9 container: a non-empty detail FALLBACK
  // implies a detail PRIMARY (the primary resolution above already falls back
  // to the same local candidate when the registry has none), so whenever the
  // first term below is non-empty, heroIsDetail is true and the container is
  // already sized 25:6. The reachable cross-tier case is the converse — a
  // registry detail banner failing with only local Browse art on disk — where
  // borrowing the other tier's art beats no art, the ratio stays keyed on the
  // primary (heroIsDetail above), and object-cover crops rather than distorts.
  const heroDetailFallback = resolvedMode === 'dark'
    ? (app.heroImageDetailDarkFallback || app.heroImageDetailFallback || '')
    : (app.heroImageDetailFallback || app.heroImageDetailDarkFallback || '')
  const heroBrowseFallback = resolvedMode === 'dark'
    ? (app.heroImageDarkFallback || app.heroImageFallback || '')
    : (app.heroImageFallback || app.heroImageDarkFallback || '')
  const heroSrcFallback = heroDetailFallback || heroBrowseFallback
  // Resolve untrusted registry metadata once and use the same normalized arrays
  // for both visibility and content. Reading the raw field for visibility would
  // render an empty titled card when a third-party index supplied a string or a
  // mixed array that the resolver correctly rejects.
  const useCases = appUseCases(app)
  const configuration = appConfiguration(app)
  // The journal entry behind the current error, when there is one: its `code` is
  // the `done` payload's machine code, journaled by `reportInstallFailure`, and
  // it selects the error box's copy below. Looked up by the message, which is
  // the journal key, so a later unrelated `setError` never inherits an install
  // failure's code.
  const errorReport = error ? findReport(error) : undefined
  // The desktop build-step refusal is permanent for this app as it is: the
  // Install / Update buttons below are disabled while its banner is up, because
  // pressing them runs the same clone and build into the same refusal. The
  // banner's dismiss clears `error`, which is how a retry is reached once the
  // app's author has shipped the packages. The disabled buttons SAY so: a
  // reader who met the banner's label alone could not tell what dismissing a
  // notice had to do with being allowed to install again, so the causality is
  // stated at the point of action too -- a `title` on the button they are
  // actually looking at.
  const installRefusedForGood = Boolean(error) && errorReport?.code === DESKTOP_BUILD_STEP_UNSUPPORTED
  // The verb follows the action the user pressed: on an INSTALLED app the button
  // beside "Installed version" is Update, and a banner saying "can't be
  // installed" with a dismiss that "re-enables Install" would describe a button
  // that is not there. Every key is resolved as a LITERAL (`check-i18n-keys.mjs`
  // verifies only a key it can read at the `i18nT()` call site), and the page
  // help sentence carries the shared explanation in `{{help}}` either way.
  const refusalHelp = i18nT('components.appstore.trustAppModal.failed_desktop_unsupported_help')
  const refusalCopy = !installRefusedForGood
    ? undefined
    : app.installed
      ? {
        title: i18nT('pages.appDetailPage.update_desktop_unsupported'),
        message: i18nT('pages.appDetailPage.update_desktop_unsupported_help', { help: refusalHelp }),
        dismiss: i18nT('pages.appDetailPage.dismiss_to_update_again'),
        disabledTitle: i18nT('pages.appDetailPage.dismiss_notice_to_update_again'),
      }
      : {
        title: i18nT('pages.appDetailPage.install_desktop_unsupported'),
        message: i18nT('pages.appDetailPage.install_desktop_unsupported_help', { help: refusalHelp }),
        dismiss: i18nT('pages.appDetailPage.dismiss_to_install_again'),
        disabledTitle: i18nT('pages.appDetailPage.dismiss_notice_to_install_again'),
      }
  const installDisabledTitle = refusalCopy?.disabledTitle

  return (
    <>
      <PageHeader title={i18nT('pages.appDetailPage.apps')} subtitle={appDisplayName(app)} />
      <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        {/* Back link */}
        <button className="flex items-center gap-1.5 text-[13px] text-muted hover:text-text mb-5 bg-transparent border-none cursor-pointer p-0 font-body transition-colors" onClick={() => navigate('/apps')}>
          <ArrowLeft size={14} /> {i18nT('pages.appDetailPage.back_to_apps')}
        </button>

        {/* In-place sync succeeded. Stated because nothing else on the page
            changes when a re-copy carries the same version. No dismiss control:
            unlike the error box below — which persists until cleared and so needs
            one — this clears itself, and a close button on a self-closing notice
            is a control whose only outcome is to race the timer. */}
        {successMsg && (
          <div className="mb-4 bg-ok/10 border border-ok/20 rounded-lg p-3 animate-rise">
            <span className="text-ok text-sm block">{successMsg}</span>
          </div>
        )}

        {/* Re-consent after an update that widened the session-approval grant.
            Warn-styled like the PR's other session-approval surfaces -- the text
            says "it is now disabled", so a green box would contradict it. Cleared
            when the user enables the app again (the action the notice asks for). */}
        {reconsentMsg && (
          <div
            role="status"
            className="mb-4 flex items-start gap-2 rounded-lg border border-warn/30 bg-warn-subtle p-3 animate-rise"
          >
            <ShieldAlert size={14} className="mt-[3px] shrink-0 text-warn" />
            <span className="text-text text-sm">{reconsentMsg}</span>
          </div>
        )}

        {app.installed && hasStagedGrants && (
          <div
            role="status"
            className="mb-4 flex flex-wrap items-start gap-3 rounded-lg border border-warn/30 bg-warn-subtle p-3 animate-rise"
          >
            <ShieldAlert size={14} className="mt-[3px] shrink-0 text-warn" />
            <div className="text-text text-sm flex-1 min-w-0 grid gap-2">
              <span>
                {app.enabled
                  ? i18nT('pages.appDetailPage.grants_reconsent_notice', { name: appDisplayName(app) })
                  : i18nT('pages.appDetailPage.grants_reconsent_notice_disabled', { name: appDisplayName(app) })}
              </span>
              {app.enabled && (
                <span className="text-[13px] text-text">{i18nT('pages.appDetailPage.grants_reconsent_keep', { name: appDisplayName(app) })}</span>
              )}
              {stagedApi.length > 0 && (
                <div>
                  <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_api')}</div>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {stagedApi.map(p => (
                      <code key={p} className="bg-bg-elevated border border-border px-1.5 py-0.5 rounded text-[11px] text-text">{p}</code>
                    ))}
                  </div>
                </div>
              )}
              {stagedEvents.length > 0 && (
                <div>
                  <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_events')}</div>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {stagedEvents.map(e => (
                      <code key={e} className="bg-bg-elevated border border-border px-1.5 py-0.5 rounded text-[11px] text-text">{e}</code>
                    ))}
                  </div>
                </div>
              )}
              {stagedLinkHosts.length > 0 && (
                <div>
                  <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_link_hosts')}</div>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {stagedLinkHosts.map(h => (
                      <code key={h} className="bg-bg-elevated border border-border px-1.5 py-0.5 rounded text-[11px] text-text">{h}</code>
                    ))}
                  </div>
                </div>
              )}
              {stagedLinkPatterns.length > 0 && (
                <div>
                  <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_link_patterns')}</div>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {stagedLinkPatterns.map(p => (
                      <code key={p} className="bg-bg-elevated border border-border px-1.5 py-0.5 rounded text-[11px] text-text break-all">{p}</code>
                    ))}
                  </div>
                </div>
              )}
              {stagedFullUrl && (
                <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_link_full_url')}</div>
              )}
              {app.enabled && (
                <span className="text-[13px] text-muted">{i18nT('pages.appDetailPage.grants_reconsent_undo', { name: appDisplayName(app) })}</span>
              )}
            </div>
            {/* Approval is offered only on a running app: on a disabled one the
                enable route would also switch the app on. Enabling it first runs
                it on the approved set, and the notice stays until approval. */}
            {app.enabled && (
            <Btn
              primary
              onClick={async () => {
                setActionLoading('approve_grants')
                const approved = [...stagedApi, ...stagedEvents, ...stagedLinkHosts, ...stagedLinkPatterns, ...(stagedFullUrl ? ['fullUrl'] : [])].join(', ')
                try {
                  await api.enableApp(app.name, false, {
                    api: stagedApi,
                    events: stagedEvents,
                    linkPreviewHosts: stagedLinkHosts,
                    linkPreviewTextPatterns: stagedLinkPatterns,
                    linkPreviewFullUrl: stagedFullUrl ? ['fullUrl'] : [],
                  })
                  setGrantsApprovedMsg(i18nT('pages.appDetailPage.grants_approved', { entries: approved }))
                  await load()
                  window.dispatchEvent(new Event('mc:apps-changed'))
                } catch (e) {
                  setError(e instanceof Error ? e.message : String(e))
                } finally {
                  setActionLoading(null)
                }
              }}
              disabled={actionLoading === 'approve_grants'}
            >
              {i18nT('pages.appDetailPage.approve_new_permissions')}
            </Btn>
            )}
          </div>
        )}
        {grantsApprovedMsg && !hasStagedGrants && (
          <div role="status" className="mb-4 bg-ok/10 border border-ok/20 rounded-lg p-3 animate-rise">
            <span className="text-ok text-sm block">{grantsApprovedMsg}</span>
          </div>
        )}

        {/* Error. No special execution-policy branch here any more: an untrusted
            third-party app is refused with `app_execution_denied`, and that
            refusal is now resolved INLINE by the consent modal (granting this
            one app) rather than by sending the user off to flip a blanket
            switch. Everything that still reaches this box is an unrecognized
            backend failure, so it renders the prose — better than swallowing
            it — plus the agent hand-off, since raw backend prose is otherwise
            a dead end. The page holds no draft (every action commits on
            click), so the hand-off is safe.

            One recognized case: the desktop build-step refusal. An app that is
            ALREADY trusted never opens the consent modal, so its refusal lands
            here, and the raw sentence alone is developer vocabulary with no hint
            that retrying is futile. The journal carries the `done` payload's code
            beside the message (`reportInstallFailure`), so the code the modal
            keys its copy on is read here too. One verb on both surfaces: the
            refusal is the install's (the build step runs at install and nowhere
            else, so the untrusted path reaches the modal's copy from this same
            Install button), and a reader who meets the modal's sentence and then
            this banner behind it must not have to wonder whether installing and
            turning on are two steps. The plain sentence beneath is shared, with
            this page's own trailing sentence naming the way back to a retry: the
            banner's dismiss is that way, so the control carries a visible label
            saying so instead of the icon-only ✕ -- nothing else on the page
            re-enables Install. The server's own sentence is NOT repeated here:
            the install-log panel beneath shows it verbatim as the streamed
            refusal line, and the agent hand-off carries it through the report,
            so the banner says the refusal once in plain words rather than three
            times in three registers. */}
        {refusalCopy ? (
          <ErrorNotice
            title={refusalCopy.title}
            message={refusalCopy.message}
            report={errorReport}
            askAgent
            onDismiss={dismissRefusal}
            dismissLabel={refusalCopy.dismiss}
            dismissLabelVisible
            className="mb-4 animate-rise"
          />
        ) : (
          <ErrorNotice message={error} askAgent onDismiss={clearError} className="mb-4 animate-rise" />
        )}

        {/* Third-party execution-trust consent. Opened when an enable OR a
            registry install is refused with code `app_execution_denied`, instead
            of surfacing the raw backend string in the error card above. */}
        <TrustAppModal
          app={consentTarget}
          pending={trust.pending}
          failed={trust.failed}
          detail={trust.detail}
          detailCode={trust.detailCode}
          granted={trust.granted}
          onCancel={trust.cancel}
          onConfirm={trust.confirm}
        />
        {consentDialog}

        {/* Uninstall confirmation modal. Click-to-dismiss on the backdrop is a
            mouse affordance; keyboard users close with Escape, and Tab stays
            inside the dialog (AppStoreDialog). */}
        {showUninstallConfirm && app && (
          <AppStoreDialog
            className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/60 backdrop-blur-xs animate-rise"
            onClose={() => setShowUninstallConfirm(false)}
            aria-label={i18nT('pages.appDetailPage.confirm_uninstall')}
          >
            {/* Presentational wrapper: stops backdrop-dismiss on inner clicks. */}
            {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events */}
            <div className="bg-card border border-border rounded-xl p-6 max-w-sm w-full mx-4 shadow-xl" onClick={e => e.stopPropagation()}>
              <div className="flex items-center gap-3 mb-4">
                <div className="w-10 h-10 rounded-xl bg-danger/10 flex items-center justify-center">
                  <Trash2 size={20} className="text-danger" />
                </div>
                <div>
                  <div className="font-medium text-text">{i18nT('pages.appDetailPage.uninstall')} {appDisplayName(app)}?</div>
                  <div className="text-[12px] text-muted">{i18nT('pages.appDetailPage.v')}{app.installedVersion || app.version}</div>
                </div>
              </div>

              <p className="text-[13px] text-muted mb-4">{i18nT('pages.appDetailPage.this_will_remove_the_app_and_all_its_registered')}</p>

              <label htmlFor="keep-app-data" className="flex items-center gap-2 text-[13px] text-muted mb-5 cursor-pointer select-none">
                <input id="keep-app-data" type="checkbox" checked={keepData} onChange={e => setKeepData(e.target.checked)} className="rounded" aria-label={i18nT('pages.appDetailPage.keep_app_data')} />
                {i18nT('pages.appDetailPage.keep_app_data')}
              </label>

              <div className="flex items-center gap-2 justify-end">
                <Btn onClick={() => setShowUninstallConfirm(false)}>{i18nT('pages.appDetailPage.cancel')}</Btn>
                <Btn danger onClick={confirmUninstall} disabled={actionLoading === 'uninstall'}>
                  {actionLoading === 'uninstall' ? i18nT('pages.appDetailPage.removing') : i18nT('pages.appDetailPage.uninstall')}
                </Btn>
              </div>
            </div>
          </AppStoreDialog>
        )}

        {/* Hero banner (only when the app ships one, or its local fallback survives a load failure) */}
        <HeroBanner src={heroSrc} fallbackSrc={heroSrcFallback || undefined} isDetail={heroIsDetail} />

        {/* Hero */}
        <div className="flex items-start gap-5 mb-6">
          {/* `relative` is load-bearing, not decoration: `rasterFill` absolutely
              insets the image, so without a positioned plate the icon resolves
              against the nearest positioned ancestor — there is none above this
              hero row, so it would escape to a page-level box. `overflow-hidden`
              is what makes the bled image take this plate's `rounded-2xl`. */}
          <div className="w-24 h-24 rounded-2xl bg-accent/10 flex items-center justify-center shrink-0 overflow-hidden relative">
            <AppIcon icon={app.icon} iconUrl={app.iconUrl} iconUrlDark={app.iconUrlDark} iconUrlFallback={app.iconUrlFallback} iconUrlFallbackDark={app.iconUrlFallbackDark} size={64} rasterFill />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-3 mb-1 flex-wrap">
              <span className="text-xl font-medium text-text">{appDisplayName(app)}</span>
              {app.installed && isBuiltin && <Badge variant="aim">{i18nT('pages.appDetailPage.built_in')}</Badge>}
              {app.installed && isBuiltin && <Badge variant={app.enabled ? 'ok' : 'warn'}>{app.enabled ? i18nT('pages.appDetailPage.enabled') : i18nT('pages.appDetailPage.disabled')}</Badge>}
              {app.installed && isSelfManaged && !isBuiltin && <Badge variant="ok">{i18nT('pages.appDetailPage.self_managed')}</Badge>}
              {app.installed && !isSelfManaged && !isBuiltin && <Badge variant={app.enabled ? 'ok' : 'warn'}>{app.enabled ? i18nT('pages.appDetailPage.enabled') : i18nT('pages.appDetailPage.disabled')}</Badge>}
            </div>
            <div className="text-[13px] text-muted mb-3 flex items-center gap-1 flex-wrap">
              <span>{app.author} {i18nT('pages.appDetailPage.v_2')}{app.version}</span>
              {typeof app.stargazersCount === 'number' && (
                <span className="inline-flex items-center gap-0.5">
                  · <Star size={13} className="shrink-0" role="img" aria-label={i18nT('pages.appDetailPage.github_stars')} />
                  {fmtCompact(app.stargazersCount)}
                </span>
              )}
            </div>

            <div className="mb-3">
              <AppSource app={app} sources={sourceNames} unlisted={app.installed && (app.catalogListed === false || app.origin === 'local')} />
              {app._registry && app.origin !== 'local' && registriesError && (
                <ErrorNotice message={registriesError.message} variant="inline" askAgent />
              )}
            </div>

            {/* Actions */}
            <div className="flex items-center gap-2 flex-wrap">
              {!app.installed && !clientInstall && (
                <Btn {...uiLocation('apps.detail.install')} primary onClick={handleInstall} disabled={actionLoading === 'install' || installRefusedForGood} title={installDisabledTitle}>
                  {actionLoading === 'install' ? <><Loader2 size={14} className="animate-spin" /> {i18nT('pages.appDetailPage.installing')}</> : <><Download size={14} /> {i18nT('pages.appDetailPage.install')}</>}
                </Btn>
              )}
              {!app.installed && clientInstall && (
                <div className="text-[13px] text-muted flex items-center gap-1.5"><Monitor size={14} /> {i18nT('pages.appDetailPage.requires_local_install')}</div>
              )}
              {app.installed && isBuiltin && (
                <>
                  {app.enabled ? (
                    <Btn onClick={() => handleAction('disable')} disabled={actionLoading === 'disable'}><PowerOff size={14} /> {i18nT('pages.appDetailPage.disable')}</Btn>
                  ) : (
                    /* Enable stays available in a browser: enabling is a
                       server-side state change (backend, agents and crons run
                       in the gateway) and only the app's own window needs the
                       desktop shell. The requirement is shown in TEXT here (not
                       only a hover title): a tooltip is unreachable by touch or
                       keyboard, and the bare "Desktop app" label reads as a
                       category tag rather than a requirement — and this detail
                       page is where a user decides to enable the app. The compact
                       store row / feature card keep the badge alone (the decision
                       does not happen there). */
                    <>
                    <Btn onClick={() => handleAction('enable')} disabled={actionLoading === 'enable'}><Power size={14} /> {enableLabel}</Btn>
                    {desktopOnly && (
                    <span className="text-[13px] text-muted flex items-center gap-1.5">
                      <Monitor size={14} /> {i18nT('pages.appDetailPage.desktop_app_hint')}
                    </span>
                  )}
                    </>
                  )}
                </>
              )}
              {app.installed && isSelfManaged && !isBuiltin && (
                <>
                  <div className="text-[13px] text-ok flex items-center gap-1.5"><Check size={14} /> {i18nT('pages.appDetailPage.installed_version', { version: app.installedVersion })}</div>
                  {app.updateAvailable && <Btn onClick={handleInstall} disabled={actionLoading === 'install' || installRefusedForGood} title={installDisabledTitle} className="!bg-[var(--info)] !text-info-fg hover:!opacity-80">{actionLoading === 'install' ? <><Loader2 size={14} className="animate-spin" /> {i18nT('pages.appDetailPage.updating')}</> : <><ArrowUp size={14} /> {i18nT('pages.appDetailPage.update')}</>}</Btn>}
                  {canUninstall && <Btn danger onClick={() => handleAction('uninstall')} disabled={actionLoading === 'uninstall'} title={i18nT('pages.appDetailPage.removes_kirocrew_metadata_only_the_app_itself_is')}><Trash2 size={14} /> {i18nT('pages.appDetailPage.uninstall')}</Btn>}
                </>
              )}
              {app.installed && !isSelfManaged && !isBuiltin && (
                <>
                  {app.enabled ? (
                    <Btn {...uiLocation('apps.detail.disable')} onClick={() => handleAction('disable')} disabled={actionLoading === 'disable'}><PowerOff size={14} /> {i18nT('pages.appDetailPage.disable')}</Btn>
                  ) : (
                    /* Enable stays available in a browser: enabling is a
                       server-side state change (backend, agents and crons run
                       in the gateway) and only the app's own window needs the
                       desktop shell. Replacing the button with a static claim
                       left every browser user at a dead end — and this page is
                       where store rows land, so it was the common path. Same
                       pattern as AppListRow / FeaturedSpotlight rows. */
                    <>
                    <Btn onClick={() => handleAction('enable')} disabled={actionLoading === 'enable'}><Power size={14} /> {enableLabel}</Btn>
                    {desktopOnly && (
                    /* The consequence is VISIBLE here, not only in `title`. A
                       hover tooltip does not exist on touch and is not reachable
                       by keyboard, and on its own the bare "Desktop app" label
                       reads as a category tag rather than a requirement — so the
                       one place a user decides whether to enable the app was the
                       one place the requirement could go unread. The compact
                       store row and feature card keep the badge alone: every
                       other badge in those components works that way, and the
                       decision does not happen there. */
                    <span className="text-[13px] text-muted flex items-center gap-1.5">
                      <Monitor size={14} /> {i18nT('pages.appDetailPage.desktop_app_hint')}
                    </span>
                  )}
                    </>
                  )}
                  {canUpdate && app.updateAvailable && <Btn {...uiLocation('apps.detail.update')} onClick={handleInstall} disabled={actionLoading === 'install' || installRefusedForGood} title={installDisabledTitle} className="!bg-[var(--info)] !text-info-fg hover:!opacity-80">{actionLoading === 'install' ? <><Loader2 size={14} className="animate-spin" /> {i18nT('pages.appDetailPage.updating')}</> : <><ArrowUp size={14} /> {i18nT('pages.appDetailPage.update')}</>}</Btn>}
                  {canUpdate && !app.updateAvailable && <Btn {...uiLocation('apps.detail.sync')} onClick={() => handleAction('update')} disabled={actionLoading === 'update'} title={i18nT('pages.appDetailPage.sync_app_from_its_source_directory')}><RefreshCw size={14} /> {i18nT('pages.appDetailPage.sync')}</Btn>}
                  {canUninstall && <Btn {...uiLocation('apps.detail.uninstall')} danger onClick={() => handleAction('uninstall')} disabled={actionLoading === 'uninstall'}><Trash2 size={14} /> {i18nT('pages.appDetailPage.uninstall')}</Btn>}
                </>
              )}
            </div>
          </div>
        </div>

        {/* Install log (inline, between hero and description) */}
        {showInstallLog && (
          <Card>
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                {!installDone && <Loader2 size={14} className="animate-spin text-accent" />}
                {installDone && !error && <Check size={14} className="text-ok" />}
                {installDone && error && !installRefusedForGood ? (
                  /* The failure headline is the shared surface, not a bespoke
                     "Fix with AI" button. The hand-off resolves the journal entry
                     `reportInstallFailure` wrote for this exact message, so it
                     carries the log tail the old button pasted by hand. The
                     message itself already shows in the page banner above; this
                     row states the outcome. */
                  <ErrorNotice
                    variant="inline"
                    message={i18nT('pages.appDetailPage.install_failed')}
                    report={findReport(error)}
                    askAgent
                  />
                ) : (
                  /* Under the permanent desktop refusal's banner the row is a
                     plain title naming what the panel is -- the log -- not the
                     outcome: the banner already carries the explanation, the
                     agent hand-off and the one dismiss, and a second red notice
                     with its own copies of both left a reader unable to tell why
                     there were two or which dismiss clears what. The log stays
                     reachable beneath it. The title takes the verb of the path
                     the banner took (Update on an installed app), so the panel
                     under an update-verbed banner is not named for an install. */
                  <CardTitle>
                    {!installDone
                      ? i18nT('pages.appDetailPage.installing')
                      : error
                        ? app.installed
                          ? i18nT('pages.appDetailPage.update_log')
                          : i18nT('pages.appDetailPage.install_log')
                        : i18nT('pages.appDetailPage.install_complete')}
                  </CardTitle>
                )}
              </div>
              <div className="flex items-center gap-2">
                {/* One dismiss while the refusal banner is up -- the banner's,
                    which drops this panel too. */}
                {installDone && !installRefusedForGood && (
                  <button className="text-muted hover:text-text transition-colors p-1" onClick={() => setShowInstallLog(false)} aria-label={i18nT('pages.appDetailPage.close')}>
                    <X size={14} />
                  </button>
                )}
              </div>
            </div>
            <pre
              ref={installLogRef}
              className="bg-bg border border-border rounded-lg p-3 text-[12px] text-muted whitespace-pre-wrap font-mono max-h-64 overflow-y-auto"
            >{installLog || (installDone ? '' : i18nT('pages.appDetailPage.starting_install'))}</pre>
          </Card>
        )}

        {/* Client install instructions */}
        {clientInstall && clientInstall.shell && (() => {
          // Replace template variables with actual values
          const gatewayUrl = window.location.origin
          const gatewayHost = serverHostname || '<your-cloud-desktop-host>'
          const replaceVars = (s: string) => s
            .replace(/\{\{gateway_url\}\}/g, gatewayUrl)
            .replace(/\{\{gateway_host\}\}/g, gatewayHost)
          const resolvedShell = replaceVars(clientInstall.shell!)
          const resolvedPostInstall = replaceVars(clientInstall.postInstall || '')
          return (
          <Card>
            <div className="flex items-start gap-3">
              <div className="w-10 h-10 rounded-xl bg-accent/10 flex items-center justify-center shrink-0 mt-0.5">
                <Terminal size={20} className="text-accent" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="font-medium text-text mb-1">{i18nT('pages.appDetailPage.install_on_your_mac')}</div>
                <p className="text-[13px] text-muted mb-3">
                  {i18nT('pages.appDetailPage.this_app_requires_macos_and_needs_to_be_installe')}
                </p>
                <div className="relative group/cmd">
                  <pre className="bg-bg border border-border rounded-lg p-3 pr-10 text-[13px] font-mono text-text overflow-x-auto whitespace-pre-wrap break-all">{resolvedShell}</pre>
                  <button
                    className="absolute top-2 right-2 p-1.5 rounded-md bg-bg-elevated border border-border text-muted hover:text-text hover:border-accent/40 transition-all opacity-0 group-hover/cmd:opacity-100 [@media(hover:none)]:opacity-100"
                    aria-label={i18nT('pages.appDetailPage.copy_command')}
                    onClick={async () => {
                      // Gate the confirmation on the boolean: a tick over an
                      // unchanged clipboard is worse than no affordance.
                      if (await copyCode(resolvedShell)) {
                        setCopied(true)
                        setTimeout(() => setCopied(false), 2000)
                      }
                    }}
                  >
                    {copied ? <Check size={14} className="text-ok" /> : <Copy size={14} />}
                  </button>
                </div>
                {resolvedPostInstall && (
                  <p className="text-[12px] text-muted mt-2">
                    {i18nT('pages.appDetailPage.after_installation_run')} <code className="bg-bg-elevated px-1.5 py-0.5 rounded text-[12px]">{resolvedPostInstall}</code>
                  </p>
                )}
                <p className="text-[12px] text-muted mt-2">
                  {i18nT('pages.appDetailPage.once_launched_the_app_will_automatically_connect')}
                </p>
              </div>
            </div>
          </Card>
          )
        })()}

        {/* Description */}
        <Card>
          <p className="text-sm text-muted leading-relaxed">{appDescription(app)}</p>
        </Card>

        {/* Screenshots */}
        {(() => {
          const dark = app.screenshotsDark || []
          const light = app.screenshots || []
          const useDark = resolvedMode === 'dark' && dark.length > 0
          // The fallback list must come from the SAME theme family the
          // primary list came from: the two arrays pair by index against the
          // same declared manifest field, so mixing families (dark primary,
          // light fallback) could pair a thumbnail with a different image
          // entirely. When the matching family has no local list, the gallery
          // stays default-inert, exactly as before #6864.
          return (
            <ScreenshotGallery
              screenshots={useDark ? dark : light}
              fallbacks={useDark ? app.screenshotsDarkFallback : app.screenshotsFallback}
            />
          )
        })()}

        {/* Concise operator guidance, kept separate from the marketing feature list. */}
        {(useCases.length > 0 || configuration.length > 0) && (
          <div className="grid grid-cols-[repeat(auto-fit,minmax(280px,1fr))] gap-4 mb-4">
            {useCases.length > 0 && (
              <Card>
                <CardTitle>
                  <Target className="lucide-inline text-accent" />{' '}
                  {i18nT('pages.appDetailPage.use_cases')}
                </CardTitle>
                <div className="grid gap-2 mt-2">
                  {useCases.map((item, i) => (
                    <div key={i} className="flex items-start gap-2.5 text-[13px] text-text">
                      <span className="mt-[7px] size-1.5 rounded-full bg-accent shrink-0" />
                      <span>{item}</span>
                    </div>
                  ))}
                </div>
              </Card>
            )}
            {configuration.length > 0 && (
              <Card>
                <CardTitle>
                  <Settings2 className="lucide-inline text-accent" />{' '}
                  {i18nT('pages.appDetailPage.configuration')}
                </CardTitle>
                <div className="grid gap-2 mt-2">
                  {configuration.map((item, i) => (
                    <div key={i} className="flex items-start gap-2.5 text-[13px] text-text">
                      <span className="mt-[7px] size-1.5 rounded-full bg-accent shrink-0" />
                      <span>{item}</span>
                    </div>
                  ))}
                </div>
              </Card>
            )}
          </div>
        )}

        {/* Features */}
        {(app.highlights || []).length > 0 && (
          <Card>
            <CardTitle>{i18nT('pages.appDetailPage.features')}</CardTitle>
            <div className="grid gap-2 mt-2">
              {appHighlights(app).map((h, i) => (
                <div key={i} className="flex items-start gap-2.5 text-[13px] text-text">
                  <Check size={13} className="text-ok mt-0.5 shrink-0" />
                  <span>{h}</span>
                </div>
              ))}
            </div>
          </Card>
        )}

        {/* Info grid */}
        <div className="grid grid-cols-[repeat(auto-fit,minmax(280px,1fr))] gap-4 mt-4">
          {/* Permissions (transparency) */}
          {app.manifest?.permissions && (
            <Card>
              <CardTitle>{i18nT('pages.appDetailPage.permissions')}</CardTitle>
              <div className="grid gap-2 mt-2 text-[13px]">
                {(app.manifest.permissions.api || []).length > 0 && (
                  <div>
                    <div className="text-muted text-[11px] uppercase tracking-wider mb-1">{i18nT('pages.appDetailPage.api_access')}</div>
                    <div className="flex flex-wrap gap-1">
                      {(app.manifest.permissions.api || []).map((p: string) => (
                        <code key={p} className="bg-bg-elevated border border-border px-1.5 py-0.5 rounded text-[11px] text-text">
                          {p}
                          {stagedApi.includes(p) && <span className="ml-1 text-warn">· {i18nT('pages.appDetailPage.grant_not_approved')}</span>}
                        </code>
                      ))}
                    </div>
                  </div>
                )}
                {(app.manifest.permissions.events || []).length > 0 && (
                  <div>
                    <div className="text-muted text-[11px] uppercase tracking-wider mb-1">{i18nT('pages.appDetailPage.websocket_events')}</div>
                    <div className="flex flex-wrap gap-1">
                      {(app.manifest.permissions.events || []).map((e: string) => (
                        <code key={e} className="bg-bg-elevated border border-border px-1.5 py-0.5 rounded text-[11px] text-text">
                          {e}
                          {stagedEvents.includes(e) && <span className="ml-1 text-warn">· {i18nT('pages.appDetailPage.grant_not_approved')}</span>}
                        </code>
                      ))}
                    </div>
                  </div>
                )}
                {(app.manifest.permissions.mcpTools || []).length > 0 && (
                  <div>
                    <div className="text-muted text-[11px] uppercase tracking-wider mb-1">{i18nT('pages.appDetailPage.mcp_tools')}</div>
                    <div className="flex flex-wrap gap-1">
                      {(app.manifest.permissions.mcpTools || []).map((t: string) => (
                        <code key={t} className="bg-ok-subtle border border-ok/20 px-1.5 py-0.5 rounded text-[11px] text-ok-text">{t}</code>
                      ))}
                    </div>
                  </div>
                )}
                {app.manifest.permissions.sessionApproval && (
                  <div className="rounded-md border border-warn/30 bg-warn-subtle px-2.5 py-2">
                    {/* Plain words first: the manifest key alone told a reader nothing
                        about what the app can do to their sessions. The key stays as
                        the secondary label so it matches the manifest they may read. */}
                    <div className="flex items-start gap-2 text-text">
                      <ShieldAlert size={13} className="mt-[2px] shrink-0 text-warn" />
                      <span>{i18nT('pages.appDetailPage.session_approval_desc')}</span>
                    </div>
                    <SessionApprovalModes
                      className="mt-1.5 text-[12px]"
                      label={i18nT('pages.appDetailPage.session_approval_modes')}
                    />
                    <div className="mt-1.5 text-[11px] text-muted">
                      {i18nT('pages.appDetailPage.session_approval_manifest_key')}: <code>sessionApproval</code>
                    </div>
                  </div>
                )}
                <div className="flex flex-wrap gap-3 text-[12px] text-muted mt-1">
                  {app.manifest.permissions.storage && <span className="flex items-center gap-1">{i18nT('pages.appDetailPage.storage_yes')}</span>}
                  {app.manifest.permissions.cron && <span className="flex items-center gap-1">{i18nT('pages.appDetailPage.cron_yes')}</span>}
                  {app.manifest.permissions.network && <span className="flex items-center gap-1">{i18nT('pages.appDetailPage.network_yes')}</span>}
                  {app.manifest.permissions.memory && <span className="flex items-center gap-1">{i18nT('pages.appDetailPage.memory')} {String(app.manifest.permissions.memory)}</span>}
                </div>
              </div>
            </Card>
          )}

          {/* MCP Servers */}
          {app.manifest?.mcpServers && Object.keys(app.manifest.mcpServers).length > 0 && (
            <Card>
              <CardTitle>{i18nT('pages.appDetailPage.mcp_servers')}</CardTitle>
              <div className="grid gap-2 mt-2 text-[13px]">
                {Object.entries(app.manifest.mcpServers).map(([sName, sConfig]) => (
                  <div key={sName} className="bg-bg-elevated border border-border rounded-md px-2.5 py-2">
                    <div className="font-mono font-medium text-text text-[12px]">{sName}</div>
                    {sConfig.url && <div className="text-muted text-[11px] mt-0.5">{sConfig.url}</div>}
                    {sConfig.command && <div className="text-muted text-[11px] mt-0.5">{sConfig.command}</div>}
                    {(sConfig.autoApprove || []).length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1.5">
                        {(sConfig.autoApprove || []).map((t: string) => (
                          <span key={t} className="bg-ok-subtle border border-ok/20 px-1 py-0 rounded text-[10px] text-ok-text">{t}</span>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </Card>
          )}

          {/* Tags */}
          {(app.tags || []).length > 0 && (
            <Card>
              <CardTitle>{i18nT('pages.appDetailPage.tags')}</CardTitle>
              <div className="flex items-center gap-1.5 flex-wrap mt-2">
                {app.tags!.map(t => (
                  <span key={t} className="bg-bg-elevated border border-border px-2 py-0.5 rounded text-[11px] text-muted">{t}</span>
                ))}
              </div>
            </Card>
          )}

          {/* Resources (installed only) */}
          {app.installed && (agents.length > 0 || skills.length > 0 || crons.length > 0) && (
            <Card>
              <CardTitle>{i18nT('pages.appDetailPage.resources')}</CardTitle>
              <div className="grid gap-1.5 mt-2 text-[13px]">
                {agents.length > 0 && (
                  <div className="flex items-start gap-2 text-muted">
                    <Bot size={13} className="mt-0.5 shrink-0" />
                    <div>{agents.map((a: string) => a.split('/').pop()?.replace('.json', '')).join(', ')}</div>
                  </div>
                )}
                {skills.length > 0 && (
                  <div className="flex items-start gap-2 text-muted">
                    <Zap size={13} className="mt-0.5 shrink-0" />
                    <div>{skills.map((s: string) => s.split('/').pop()).join(', ')}</div>
                  </div>
                )}
                {crons.length > 0 && (
                  <div className="flex items-start gap-2 text-muted">
                    <Clock size={13} className="mt-0.5 shrink-0" />
                    <div>{crons.map((c) => c.name).join(', ')}</div>
                  </div>
                )}
              </div>
            </Card>
          )}

          {/* Metadata */}
          <Card>
            <CardTitle>{i18nT('pages.appDetailPage.details')}</CardTitle>
            <div className="grid gap-1.5 mt-2 text-[13px] text-muted">
              {app.repo && (
                sourceRepoUrl ? (
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <span className="shrink-0">{i18nT('pages.appDetailPage.repository')}</span>
                    <a
                      href={sourceRepoUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-accent no-underline hover:underline break-all"
                    >
                      {app.repo}
                      <ExternalLink size={12} className="shrink-0" aria-hidden="true" />
                    </a>
                  </div>
                ) : (
                  <div>{i18nT('pages.appDetailPage.repository')} {app.repo}</div>
                )
              )}
              {issuesUrl && (
                <div className="flex items-center gap-1.5 flex-wrap">
                  <a
                    href={issuesUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-accent no-underline hover:underline"
                  >
                    {i18nT('pages.appDetailPage.report_issue')}
                    <ExternalLink size={12} className="shrink-0" aria-hidden="true" />
                  </a>
                </div>
              )}
              {typeof app.stargazersCount === 'number' && <div>{i18nT('pages.appDetailPage.github_stars_2', { value: fmtNumber(app.stargazersCount) })}</div>}
              {app.author && <div>{i18nT('pages.appDetailPage.author')} {app.author}</div>}
              {repoUrl && (contribLoading || contributors.length > 0 || contribError) && (
                <div className="flex items-start gap-2 flex-wrap">
                  <span className="shrink-0">{i18nT('pages.appDetailPage.contributors')}</span>
                  {contribLoading ? (
                    <div className="flex gap-1.5">
                      {[0, 1, 2].map(i => (
                        <span
                          key={i}
                          className="inline-block h-[22px] w-16 rounded-full animate-pulse"
                          style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}
                        />
                      ))}
                    </div>
                  ) : contribError ? (
                    /* Read-only details row: the hand-off has nothing to lose. */
                    <ErrorNotice message={contribError} variant="inline" askAgent />
                  ) : (
                    <div className="flex items-center gap-1.5 flex-wrap">
                      {contributors.slice(0, 6).map(c => (
                        <span
                          key={c.login}
                          title={c.name}
                          className="inline-flex items-center gap-1.5 rounded-full pl-0.5 pr-2 py-0.5"
                          style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: 'var(--text)' }}
                        >
                          <span
                            className="relative inline-flex h-[18px] w-[18px] items-center justify-center overflow-hidden rounded-full text-[10px]"
                            style={{ background: 'var(--accent-subtle)', color: 'var(--accent)' }}
                          >
                            <span>{(c.name || c.login).slice(0, 1).toUpperCase()}</span>
                            {c.avatarUrl && /^https:\/\/[^"')(\s]+$/.test(c.avatarUrl) && (
                              <span
                                aria-hidden="true"
                                className="absolute inset-0 rounded-full"
                                style={{ backgroundImage: `url("${c.avatarUrl}")`, backgroundSize: 'cover', backgroundPosition: 'center' }}
                              />
                            )}
                          </span>
                          <span className="text-[12px]">{c.name}</span>
                        </span>
                      ))}
                      <a
                        href={`${repoUrl.replace(/\.git$/, '').replace(/\/$/, '')}/graphs/contributors`}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="inline-flex items-center rounded-full px-2 py-0.5 text-[12px] no-underline"
                        style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: 'var(--muted)' }}
                      >
                        {i18nT('pages.appDetailPage.all_contributors')}
                      </a>
                    </div>
                  )}
                </div>
              )}
              {app.installedAt && <div>{i18nT('pages.appDetailPage.installed')} {fmtDateNumeric(app.installedAt)}</div>}
              {app.origin && <div>{i18nT('pages.appDetailPage.origin')} {app.origin} {i18nT('pages.appDetailPage.resources_2')} {app.resources || 'gateway'} {i18nT('pages.appDetailPage.lifecycle')} {app.lifecycle || 'gateway'}</div>}
              {app.manifest?.minKiroCrewVersion && <div>{i18nT('pages.appDetailPage.min_kirocrew_v')}{app.manifest.minKiroCrewVersion}</div>}
              {app.platform?.os && <div>{i18nT('pages.appDetailPage.platform')} {app.platform.os.join(', ')}</div>}
            </div>
          </Card>
        </div>
      </div>
    </>
  )
}
