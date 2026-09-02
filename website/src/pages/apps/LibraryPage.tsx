/**
 * LibraryPage — installed-app management, split out of the old AppsPage
 * (PR1 App Store split; route `/apps/library`).
 *
 * The list presentation is a macOS-Launchpad-style icon grid (PR3, approved
 * mockup frame #a): each installed app is a `LaunchpadTile` whose pin badge
 * toggles whether the app appears in the sidebar (persisted as the HIDDEN
 * set under `mc-app-nav-hidden`, owned by `lib/appNavHidden.ts`), with a
 * hover action bar carrying the management verbs the old cards offered.
 * Search and the uninstall preview flow survive from the card list. Data
 * identity (installed list, update map, mc:apps-changed announcement) lives
 * in the shared `useAppsData` hook so this page and Discover cannot drift.
 *
 * Pending updates surface here as a light one-line hint linking to the
 * Discover Updates sub-page (`/apps/-/updates`), which owns the update
 * worklist and Update All (PR2 App Store split). Per-tile Update stays on
 * the action bar via the shared `useAppUpdates` hook.
 */
import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  Package, Bot, Zap, Clock, Lock, Trash2, X, ArrowUp, Compass,
  AlertTriangle, PowerOff,
} from 'lucide-react'
import { api } from '../../api/client'
import { appNavTarget } from '../../appNav'
import { Btn, EmptyState, PageHeader, SearchInput } from '../../components/ui'
import { recordEvent } from '../../rum'
import TrustAppModal, { isTrustDeniedError } from '../../components/appstore/TrustAppModal'
import type { InstalledApp } from '../../components/appstore/types'
import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
import ErrorBoundary from '../../components/ErrorBoundary'
import { toggleAppNavHidden, useAppNavHidden } from '../../lib/appNavHidden'
import useAppsData from './useAppsData'
import { useAppActions } from './useAppActions'
import { useAppUpdates } from './useAppUpdates'
import { cardDataKey } from './cardDataKey'
import LaunchpadTile from './LaunchpadTile'

/** Uninstall preview payload (mirrors ``api.uninstallPreview`` return shape). */
type UninstallPreview = Awaited<ReturnType<typeof api.uninstallPreview>>
type RemovableDep = UninstallPreview['dependencies']['removable'][number]
type SharedDep = UninstallPreview['dependencies']['shared'][number]
type UserInstalledDep = UninstallPreview['dependencies']['userInstalled'][number]

export default function LibraryPage() {
  const navigate = useNavigate()
  const {
    apps, appsLoading, appsError, registryError,
    browseApps, installedApps, updatables, announceAppsChanged,
  } = useAppsData()

  const [query, setQuery] = useState('')
  const [successMsg, setSuccessMsg] = useState('')
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  // `openCommand` apps opened from a remote/headless gateway cannot launch
  // here — the backend answers `{remote: true, command}` and the user runs
  // the command locally instead.
  const [remoteCmd, setRemoteCmd] = useState('')

  const {
    setError, displayError, dismissError,
    openDetail, updateApp, trustTarget, runEnable, trust,
  } = useAppActions({ apps, browseApps, appsError, registryError, announceAppsChanged })

  // Transient success surface for the shared update hook: the hook reports
  // outcomes, this page owns display + auto-dismiss (same 4s the other
  // toasts here use).
  const showSuccess = (msg: string) => {
    setSuccessMsg(msg)
    setTimeout(() => setSuccessMsg(''), 4000)
  }

  // Per-row update comes from the shared useAppUpdates hook (PR2), so this
  // page and the Discover Updates sub-page cannot drift on how an update
  // behaves. The recorded-source routing comment lives there. Update All is
  // the Updates sub-page's affordance now — this page only hints (below).
  const { updatingAll, updatePending, runUpdate } = useAppUpdates({
    apps, updatables, announceAppsChanged, updateApp,
    setError, setSuccess: showSuccess,
  })

  // Which sidebar rows the user unpinned (`mc-app-nav-hidden`) — held as
  // React state, not a raw read, so a pin toggle repaints every tile's badge
  // at once. The module's own event covers same-tab writes (this page's
  // toggles included, via the module's dispatch-on-write); the `storage`
  // listener covers a toggle made in ANOTHER tab — the same two-listener
  // shape App.tsx's sidebar filter uses.
  const navHidden = useAppNavHidden()

  // Pin toggle — resolve the tile's app to the SAME nav id the sidebar rows
  // carry (`appNavTarget(app).id`, the rail's own derivation) and flip it in
  // the persisted hidden set. The module's write dispatches the sync event,
  // which feeds the state above and App.tsx's sidebar filter — no local
  // set-state here. A tile without a nav target never offers the toggle
  // (`pinnable` below), so the null branch is only a race guard.
  const togglePin = (name: string) => {
    const app = installedApps.find(a => a.name === name)
    const id = app ? appNavTarget(app)?.id : undefined
    if (id) toggleAppNavHidden(id)
  }

  // Uninstall confirmation state
  const [uninstallTarget, setUninstallTarget] = useState<InstalledApp | null>(null)
  const [keepData, setKeepData] = useState(true)
  const [uninstallPreview, setUninstallPreview] = useState<UninstallPreview | null>(null)
  const [keepSpecific, setKeepSpecific] = useState<Set<string>>(new Set())
  // Resource lists the uninstall dialog itemises, derived once so the count it
  // prints and the check that decides to print it read the same value. Reading
  // the list twice — a `?.` gate here, a `!` assertion there — is the shape that
  // produced #3689; `normalizeInstalledApp` means the fallback is now only for a
  // record that never reached the fetch boundary.
  const uninstallAgents = uninstallTarget?.manifest?.agents || []
  const uninstallSkills = uninstallTarget?.manifest?.skills || []
  const uninstallCrons = uninstallTarget?.manifest?.crons || []

  const filteredInstalled = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return installedApps
    return installedApps.filter(a =>
      a.name.toLowerCase().includes(q)
      || (a.displayName || '').toLowerCase().includes(q)
      || (a.manifest?.description || '').toLowerCase().includes(q)
      || (a.manifest?.tags || []).some(t => t.toLowerCase().includes(q)))
  }, [installedApps, query])

  // ---- Actions --------------------------------------------------------------
  // Detail navigation, update routing, the trust-consent target, and the
  // single enable path come from useAppActions — shared with DiscoverPage so
  // the two pages cannot drift on how an action behaves.

  const handleAction = async (name: string, action: 'enable' | 'disable' | 'uninstall' | 'update') => {
    // Intercept uninstall to show confirmation modal with preview
    if (action === 'uninstall') {
      const app = apps.find(a => a.name === name)
      if (app) {
        setUninstallTarget(app)
        setKeepData(true)
        setKeepSpecific(new Set())
        // Fetch uninstall preview (best-effort — dialog works without it)
        try {
          setUninstallPreview(await api.uninstallPreview(name))
        } catch {
          setUninstallPreview(null)
        }
      }
      return
    }
    // Update routing (recorded-source dispatch, per-app pending state, the
    // Update-All concurrency guard) is the shared hook's contract now.
    if (action === 'update') return runUpdate(name)
    setActionLoading(`${name}:${action}`)
    setError('')
    try {
      if (action === 'enable') await runEnable(name)
      else if (action === 'disable') await api.disableApp(name)
      announceAppsChanged()
    } catch (e) {
      if (action === 'enable' && isTrustDeniedError(e)) trust.open(trustTarget(name))
      else setError((e as Error)?.message || i18nT('pages.appsPage.action_failed', { action, name }))
    } finally {
      setActionLoading(null)
    }
  }

  const confirmUninstall = async () => {
    if (!uninstallTarget) return
    const name = uninstallTarget.name
    setActionLoading(`${name}:uninstall`)
    setError('')
    try {
      await api.uninstallApp(name, keepData, false, Array.from(keepSpecific))
      recordEvent('app_uninstall', { app: name, version: uninstallTarget.version })
      announceAppsChanged()
    } catch (e) {
      setError((e as Error)?.message || i18nT('pages.appsPage.failed_to_uninstall', { name }))
    } finally {
      setActionLoading(null)
      setUninstallTarget(null)
      setUninstallPreview(null)
    }
  }

  return (
    <>
      {/* Standard page header with a right-side actions slot: search only —
          no SegmentedControl (the split made Discover and Library pages), and
          the Sources gear stays on Discover (page-layout-pattern). */}
      <PageHeader
        title={i18nT('pages.appsPage.library')}
        subtitle={i18nT('pages.appsPage.manage_your_installed_apps')}
        actions={
          <SearchInput
            placeholder={i18nT('pages.appsPage.search_library')}
            value={query}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) => setQuery(e.target.value)}
            className="w-[220px]"
            aria-label={i18nT('pages.appsPage.search_library')}
          />
        }
      />

      <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        {/* Notifications. No hand-off on the error notice: management actions
            share this page — navigating away would discard in-flight state. */}
        {displayError && (
          <ErrorNotice
            message={displayError}
            onDismiss={dismissError}
            className="mb-4 animate-rise"
          />
        )}
        {successMsg && (
          <div className="mb-4 bg-bg-elevated border rounded-lg p-3 flex items-center gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--ok) 45%, transparent)' }}>
            <span className="text-text text-sm flex-1">{successMsg}</span>
            <button aria-label={i18nT('pages.appsPage.dismiss_message')} className="text-muted hover:text-text text-sm" onClick={() => setSuccessMsg('')}><X className="lucide-inline" /></button>
          </div>
        )}

        {remoteCmd && (
          <div
            className="mb-4 bg-accent/10 border border-accent/20 rounded-lg p-3 text-[13px] animate-rise"
            ref={el => {
              // The clicked tile can be scrolled far below this notice; without
              // bringing it into view the Open click looks like a no-op on a
              // remote gateway (UX finding: off-locus feedback).
              el?.scrollIntoView({ block: 'nearest' })
            }}
          >
            <div className="flex items-start justify-between gap-2">
              <div>
                <span className="text-text font-medium">{i18nT('components.appstore.installedAppCard.remote_environment_detected')}</span>
                <p className="text-muted mt-1">{i18nT('components.appstore.installedAppCard.run_this_on_your_local_machine')}</p>
                <code className="block mt-1.5 bg-bg-elevated px-2 py-1 rounded text-[12px] font-mono select-all">{remoteCmd}</code>
              </div>
              <button aria-label={i18nT('components.appstore.installedAppCard.dismiss')} className="text-muted hover:text-text text-sm shrink-0" onClick={() => setRemoteCmd('')}><X className="lucide-inline" /></button>
            </div>
          </div>
        )}

        {/* Third-party execution-trust consent. Opened when an enable is
            refused with code `app_execution_denied`, instead of surfacing the
            raw backend string in the error card above. */}
        <TrustAppModal
          app={trust.target}
          pending={trust.pending}
          failed={trust.failed}
          granted={trust.granted}
          onCancel={trust.cancel}
          onConfirm={trust.confirm}
        />

        {/* Uninstall confirmation modal. The backdrop closes on click (mouse
            convenience); keyboard users press Escape (handled) or the Cancel
            button inside. The inner card's onClick only stops propagation so a
            click inside doesn't bubble to the backdrop-close — it is not a user
            interaction, hence the scoped disables. */}
        {uninstallTarget && (
          // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
          <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/60 backdrop-blur-sm animate-rise"
            onClick={() => { setUninstallTarget(null); setUninstallPreview(null) }}
            onKeyDown={e => { if (e.key === 'Escape') { setUninstallTarget(null); setUninstallPreview(null) } }}
            tabIndex={-1} ref={el => el?.focus()} role="dialog" aria-modal="true" aria-label={i18nT('pages.appsPage.confirm_uninstall')}
          >
            {/* Stop clicks inside the dialog card from reaching the backdrop's
                dismiss handler; presentation-only, no interaction of its own. */}
            <div role="presentation" className="bg-card border border-border rounded-xl p-6 max-w-md w-full mx-4 shadow-xl" onClick={e => e.stopPropagation()}>
              <div className="flex items-center gap-3 mb-4">
                <div className="w-10 h-10 rounded-xl bg-danger/10 flex items-center justify-center">
                  <Trash2 size={20} className="text-danger" />
                </div>
                <div>
                  <div className="font-medium text-text">{i18nT('pages.appsPage.uninstall')} {uninstallTarget.displayName || uninstallTarget.name}?</div>
                  <div className="text-[12px] text-muted">{i18nT('pages.appsPage.v')}{uninstallTarget.version}</div>
                </div>
              </div>

              <p className="text-[13px] text-muted mb-3">{i18nT('pages.appsPage.this_will_remove_all_resources_provided_by_this')}</p>
              <div className="text-[13px] text-text mb-4 space-y-1">
                {uninstallTarget.resources === 'app' && !uninstallTarget.manifest?.setup?.onUninstall && uninstallTarget.origin !== 'registry' && (
                  <div className="bg-bg-elevated border border-border rounded-md px-2.5 py-2 text-[12px] text-muted mb-2">
                    {i18nT('pages.appsPage.this_is_a_self_managed_app_only_kirocrew_metadat')}
                  </div>
                )}
                {uninstallTarget.manifest?.setup?.onUninstall && (
                  <div className="bg-danger/5 border border-danger/20 rounded-md px-2.5 py-2 text-[12px] text-muted mb-2">
                    {i18nT('pages.appsPage.this_app_has_an_uninstall_script_that_will_run_b')}
                  </div>
                )}
                {uninstallTarget.origin === 'registry' && (
                  <div className="bg-bg-elevated border border-border rounded-md px-2.5 py-2 text-[12px] text-muted mb-2">
                    {uninstallTarget.resources === 'app' ? i18nT('pages.appsPage.uninstall_removes_metadata_secret_and_source') : i18nT('pages.appsPage.uninstall_removes_metadata_and_source')}{uninstallTarget.resources === 'app' && !uninstallTarget.manifest?.setup?.onUninstall ? ' ' + i18nT('pages.appsPage.the_app_itself_is_managed_externally') : ''}
                  </div>
                )}
                {uninstallTarget.origin !== 'registry' && uninstallTarget.resources === 'app' && uninstallTarget.manifest?.setup?.onUninstall && (
                  <div className="bg-bg-elevated border border-border rounded-md px-2.5 py-2 text-[12px] text-muted mb-2">
                    {i18nT('pages.appsPage.not_installed_from_apps_your_local_source_code_w')}
                  </div>
                )}
                {uninstallAgents.length > 0 && (
                  <div className="flex items-center gap-2"><Bot size={12} className="text-muted" /> {i18nT('pages.appsPage.agent', { count: uninstallAgents.length })}</div>
                )}
                {uninstallSkills.length > 0 && (
                  <div className="flex items-center gap-2"><Zap size={12} className="text-muted" /> {i18nT('pages.appsPage.skill', { count: uninstallSkills.length })}</div>
                )}
                {uninstallCrons.length > 0 && (
                  <div className="flex items-center gap-2"><Clock size={12} className="text-muted" /> {i18nT('pages.appsPage.cron_job', { count: uninstallCrons.length })}</div>
                )}
              </div>

              {/* Dependency preview */}
              {uninstallPreview?.dependencies && (
                (() => {
                  const deps = uninstallPreview.dependencies
                  const hasAny = (deps.removable?.length || 0) + (deps.shared?.length || 0) + (deps.userInstalled?.length || 0) > 0
                  if (!hasAny) return null
                  return (
                    <div className="mb-4">
                      <p className="text-[13px] text-muted mb-2">{i18nT('pages.appsPage.dependencies')}</p>
                      <div className="space-y-2 text-[13px]">
                        {(deps.removable || []).map((d: RemovableDep) => (
                          <div key={d.id} className="flex items-start gap-2">
                            <Trash2 size={12} className="text-danger mt-0.5 shrink-0" />
                            <div className="flex-1">
                              <div className="text-text">{d.id.split('/').pop()}</div>
                              <div className="text-[11px] text-muted">{d.reason}</div>
                              <label htmlFor={`keep-dep-${d.id}`} className="flex items-center gap-1.5 mt-1 text-[11px] text-muted cursor-pointer">
                                <input
                                  id={`keep-dep-${d.id}`}
                                  type="checkbox"
                                  aria-label={i18nT('pages.appsPage.keep_dependency', { name: d.id.split('/').pop() })}
                                  checked={keepSpecific.has(d.id)}
                                  onChange={e => {
                                    const next = new Set(keepSpecific)
                                    if (e.target.checked) next.add(d.id); else next.delete(d.id)
                                    setKeepSpecific(next)
                                  }}
                                  className="rounded"
                                />
                                {i18nT('pages.appsPage.keep_this_dependency')}
                              </label>
                            </div>
                          </div>
                        ))}
                        {(deps.shared || []).map((d: SharedDep) => (
                          <div key={d.id} className="flex items-start gap-2">
                            <Lock size={12} className="text-muted mt-0.5 shrink-0" />
                            <div>
                              <div className="text-text">{d.id.split('/').pop()}</div>
                              <div className="text-[11px] text-muted">{i18nT('pages.appsPage.kept')} {d.reason}</div>
                            </div>
                          </div>
                        ))}
                        {(deps.userInstalled || []).map((d: UserInstalledDep) => (
                          <div key={d.id} className="flex items-start gap-2">
                            <Lock size={12} className="text-muted mt-0.5 shrink-0" />
                            <div>
                              <div className="text-text">{d.id.split('/').pop()}</div>
                              <div className="text-[11px] text-muted">{i18nT('pages.appsPage.kept_installed_by_you')}</div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )
                })()
              )}

              <label htmlFor="uninstall-keep-data" className="flex items-center gap-2 text-[13px] text-muted mb-5 cursor-pointer select-none">
                <input id="uninstall-keep-data" type="checkbox" aria-label={i18nT('pages.appsPage.keep_app_data')} checked={keepData} onChange={e => setKeepData(e.target.checked)} className="rounded" />
                {i18nT('pages.appsPage.keep_app_data')}
              </label>

              <div className="flex items-center gap-2 justify-end">
                <Btn onClick={() => { setUninstallTarget(null); setUninstallPreview(null) }}>{i18nT('pages.appsPage.cancel')}</Btn>
                <Btn danger onClick={confirmUninstall} disabled={actionLoading === `${uninstallTarget.name}:uninstall`}>
                  {actionLoading === `${uninstallTarget.name}:uninstall` ? i18nT('pages.appsPage.removing') : i18nT('pages.appsPage.uninstall')}
                </Btn>
              </div>
            </div>
          </div>
        )}

        {/* ---- Installed list ---- */}
        {appsLoading ? (
          <div className="text-center py-12 text-muted text-sm">{i18nT('pages.appsPage.loading_apps')}</div>
        ) : filteredInstalled.length === 0 ? (
          <EmptyState
            icon={<Package size={36} />}
            title={installedApps.length === 0 ? i18nT('pages.appsPage.no_apps_installed_yet') : i18nT('pages.appsPage.no_matching_apps')}
            subtitle={installedApps.length === 0
              ? i18nT('pages.appsPage.find_apps_in_the_discover_tab_or_install_from_a')
              : i18nT('pages.appsPage.try_a_different_search_term')}
            action={installedApps.length === 0
              ? (
                <Link to="/apps" className="text-accent text-sm font-medium hover:underline inline-flex items-center gap-1.5">
                  <Compass size={14} className="lucide-inline" /> {i18nT('nav.discover')}
                </Link>
              )
              : undefined}
          />
        ) : (
          <>
            {updatables.length > 0 && (
              /* Light hint, not a banner: the update WORKLIST (rows, version
                 diffs, Update All) lives on the Discover Updates sub-page —
                 this row only says updates exist and hands off there. Per-row
                 Update on the cards below still works via the shared hook. */
              <div className="mb-4 flex items-center gap-2 text-[13px] text-muted animate-rise">
                <ArrowUp size={13} className="shrink-0" aria-hidden />
                <span>{i18nT('pages.appsPage.updates_hint', { count: updatables.length })}</span>
                <Link to="/apps/-/updates" className="text-accent font-medium hover:underline shrink-0">
                  {i18nT('pages.appsPage.updates_hint_link')}
                </Link>
              </div>
            )}
            {/* Launchpad grid (mockup frame #a): ~5 columns at desktop width,
                stepping down responsively. Tiles come from `installedApps`
                (GET /api/apps records) — the Discover/Library built-in
                SURFACES are frontend nav entries, never installed-app
                records, so they cannot appear as tiles. */}
            <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 xl:grid-cols-6 gap-x-3 gap-y-7">
              {filteredInstalled.map(app => {
                // The sidebar's own eligibility/id derivation decides
                // pinnability: a tile only offers a pin for a row the rail
                // could actually show, under the byte-identical id the
                // sidebar filter matches against.
                const target = appNavTarget(app)
                return (
                  <ErrorBoundary
                    /* Full-data key (cardDataKey): the boundary latches
                       its error state, so remount when the installed app or its
                       update availability changes — e.g. when an updated payload
                       fixes a broken tile (#3719). */
                    key={cardDataKey(app)}
                    scope="apps:installed-card"
                    fallback={
                      <div className="flex flex-col items-center gap-2 rounded-xl border border-border px-1.5 pt-3.5 pb-2.5 text-center">
                        <div className="w-[58px] h-[58px] rounded-[15px] bg-bg-elevated flex items-center justify-center">
                          <AlertTriangle aria-hidden className="lucide-inline text-[var(--warn)]" />
                        </div>
                        <span className="text-[12px] font-semibold text-text-strong max-w-full truncate">{app.manifest?.displayName || app.name}</span>
                        <span className="text-[10.5px] text-muted leading-tight">{i18nT('pages.appsPage.this_app_could_not_be_displayed')}</span>
                        {/* The crashed tile removed the app's management surface, so the
                            fallback must keep one recovery path: quiet a broken enabled
                            app, or remove a disabled one entirely (locked apps cannot
                            be uninstalled). Same handlers as the healthy tile. */}
                        {app.enabled ? (
                          <Btn
                            onClick={() => handleAction(app.name, 'disable')}
                            disabled={actionLoading === `${app.name}:disable`}
                          >
                            <PowerOff size={14} /> {i18nT('components.appstore.installedAppCard.disable')}
                          </Btn>
                        ) : app.lifecycle !== 'locked' && (
                          <Btn danger onClick={() => handleAction(app.name, 'uninstall')}>
                            <Trash2 size={14} /> {i18nT('components.appstore.installedAppCard.uninstall')}
                          </Btn>
                        )}
                      </div>
                    }
                  >
                    <LaunchpadTile
                      app={app}
                      pinned={!!target && !navHidden.has(target.id)}
                      pinnable={!!target}
                      actionLoading={updatingAll
                        ? `${app.name}:update`
                        : updatePending ? `${updatePending}:update` : actionLoading}
                      onTogglePin={togglePin}
                      onAction={handleAction}
                      onOpen={() => {
                        // An `openCommand` app opens by RUNNING its command
                        // never by
                        // routing — `appNavTarget` is null for it, and the
                        // `/apps/${name}` fallback would land on a detail page
                        // pretending to be the app. Remote gateways answer
                        // `{remote: true}` with the command to run locally.
                        if (app.manifest?.openCommand) {
                          api.openApp(app.name).then((res: { remote?: boolean; command?: string; message?: string } | null) => {
                            if (res?.remote) setRemoteCmd(res.command || res.message || i18nT('components.appstore.installedAppCard.app_cannot_be_opened_kirocrew_is_running_in_a_he'))
                          }).catch(() => {})
                          return
                        }
                        navigate(target?.route || `/apps/${app.name}`)
                      }}
                      onDetail={() => openDetail(app.name)}
                    />
                  </ErrorBoundary>
                )
              })}
            </div>
          </>
        )}
      </div>
    </>
  )
}
