/**
 * LibraryPage — installed-app management, split out of the old AppsPage
 * (PR1 App Store split; route `/apps/library`).
 *
 * Pending-updates banner with Update All, search, the management cards
 * (InstalledAppCard), and the uninstall preview flow — all moved unchanged
 * from AppsPage's library branch. Data identity (installed list, update map,
 * mc:apps-changed announcement) lives in the shared `useAppsData` hook so
 * this page and Discover cannot drift.
 *
 * PR1 constraint: the update banner's behavior is UNCHANGED here — PR2 owns
 * any banner changes.
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
import InstalledAppCard from '../../components/appstore/InstalledAppCard'
import TrustAppModal, { isTrustDeniedError } from '../../components/appstore/TrustAppModal'
import { isRegistrySourced, type InstalledApp } from '../../components/appstore/types'
import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
import ErrorBoundary from '../../components/ErrorBoundary'
import useAppsData from './useAppsData'
import { useAppActions } from './useAppActions'
import { cardDataKey } from './cardDataKey'

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
  const [successLink, setSuccessLink] = useState(false)
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const [updatingAll, setUpdatingAll] = useState<{ done: number; total: number } | null>(null)

  const {
    setError, displayError, dismissError,
    openDetail, updateApp, trustTarget, runEnable, trust,
  } = useAppActions({ apps, browseApps, appsError, registryError, announceAppsChanged })

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
    // Update dispatches on the RECORDED SOURCE, mirroring ``handle_update_app``'s
    // own branch. A registry-sourced app is re-cloned from its registry and the
    // detail page owns that flow (streaming log plus the trust consent modal), so
    // it navigates there. An app installed from a path has no registry row: it is
    // refreshed in place from the directory recorded at install — the same call
    // Update All makes — and routing it at the registry instead failed every sync
    // with "not found in registry". A row absent from this list carries no source
    // to read, so it navigates and the detail page re-dispatches on the record it
    // loads. Blocked while Update All is running so the same update can't run
    // twice concurrently.
    if (action === 'update') {
      if (updatingAll) return
      const target = apps.find(a => a.name === name)
      if (!target || isRegistrySourced(target)) {
        updateApp(name)
        return
      }
    }
    setActionLoading(`${name}:${action}`)
    setError('')
    try {
      if (action === 'enable') await runEnable(name)
      else if (action === 'disable') await api.disableApp(name)
      else if (action === 'update') await api.updateApp(name)
      announceAppsChanged()
      // An in-place sync is the one action here whose success is otherwise
      // INVISIBLE: re-copying a source directory usually carries the same
      // version, so the card re-renders byte-identical and the dev cannot tell
      // whether new bytes landed. Reflect it the way `disable` already does.
      if (action === 'update') {
        const app = apps.find(a => a.name === name)
        setSuccessMsg(i18nT('pages.appsPage.synced_from_its_source_directory', {
          name: app?.displayName || name,
        }))
        setTimeout(() => setSuccessMsg(''), 4000)
      }
      // Show toast when hiding a builtin app
      if (action === 'disable') {
        const app = apps.find(a => a.name === name)
        if (app?.origin === 'builtin') {
          // A disabled overlay-less builtin leaves this list (keepInLibrary),
          // so the recovery affordance is its catalog row on the Discover
          // shelf — the toast points there and carries the link.
          setSuccessMsg(i18nT('pages.appsPage.hidden_you_can_re_enable_it_from_the_discover_ta'))
          setSuccessLink(true)
          setTimeout(() => { setSuccessMsg(''); setSuccessLink(false) }, 4000)
        }
      }
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

  const updateAll = async () => {
    if (updatingAll) return
    const targets = updatables.map(a => a.name)
    setUpdatingAll({ done: 0, total: targets.length })
    setError('')
    const failed: string[] = []
    for (let i = 0; i < targets.length; i++) {
      try {
        await api.updateApp(targets[i])
      } catch {
        failed.push(targets[i])
      }
      setUpdatingAll({ done: i + 1, total: targets.length })
    }
    setUpdatingAll(null)
    announceAppsChanged()
    if (failed.length) setError(i18nT('pages.appsPage.failed_to_update', { names: failed.join(', ') }))
    else {
      setSuccessMsg(i18nT('pages.appsPage.updated_app', { count: targets.length }))
      setTimeout(() => setSuccessMsg(''), 4000)
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
            {/* Set only by the disable-builtin toast: the row this action
                removed re-enables from its Discover catalog row, so the toast
                carries the navigation instead of naming a page and hoping. */}
            {successLink && (
              <Link to="/apps" className="text-accent text-sm font-medium hover:underline shrink-0">
                {i18nT('nav.discover')}
              </Link>
            )}
            <button aria-label={i18nT('pages.appsPage.dismiss_message')} className="text-muted hover:text-text text-sm" onClick={() => { setSuccessMsg(''); setSuccessLink(false) }}><X className="lucide-inline" /></button>
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
            {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events */}
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
              <div className="mb-4 border border-[color-mix(in_srgb,var(--info)_45%,transparent)] bg-bg-elevated rounded-lg p-3 flex items-center gap-3 animate-rise">
                <ArrowUp size={15} className="text-[var(--info)] shrink-0" />
                <span className="text-text text-sm flex-1">
                  {i18nT('pages.appsPage.update', { count: updatables.length })} {i18nT('pages.appsPage.available')}
                </span>
                <Btn
                  className="!bg-[var(--info)] !text-white hover:!opacity-80"
                  onClick={updateAll}
                  disabled={!!updatingAll}
                >
                  {updatingAll ? i18nT('pages.appsPage.updating_progress', { done: updatingAll.done, total: updatingAll.total }) : i18nT('pages.appsPage.update_all')}
                </Btn>
              </div>
            )}
            <div className="space-y-3">
              {filteredInstalled.map(app => (
                <ErrorBoundary
                  /* Full-data key (cardDataKey): the boundary latches
                     its error state, so remount when the installed app or its
                     update availability changes — e.g. when an updated payload
                     fixes a broken card (#3719). */
                  key={cardDataKey(app)}
                  scope="apps:installed-card"
                  fallback={
                    <div className="border border-border rounded-lg p-4 flex items-center gap-3">
                      <AlertTriangle aria-hidden className="lucide-inline text-[var(--warn)] shrink-0" />
                      <div className="min-w-0 text-sm flex-1">
                        <span className="font-medium text-text">{app.manifest?.displayName || app.name}</span>
                        <span className="text-muted ml-2">{i18nT('pages.appsPage.this_app_could_not_be_displayed')}</span>
                      </div>
                      {/* The crashed card removed the app's management surface, so the
                          fallback must keep one recovery path: quiet a broken enabled
                          app, or remove a disabled one entirely (locked apps cannot
                          be uninstalled). Same handlers as the healthy card. */}
                      <div className="shrink-0">
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
                    </div>
                  }
                >
                  <InstalledAppCard
                    app={app}
                    actionLoading={updatingAll ? `${app.name}:update` : actionLoading}
                    onAction={handleAction}
                    onOpen={() => navigate(appNavTarget(app)?.route || `/apps/${app.name}`)}
                    onDetail={() => openDetail(app.name)}
                  />
                </ErrorBoundary>
              ))}
            </div>
          </>
        )}
      </div>
    </>
  )
}
