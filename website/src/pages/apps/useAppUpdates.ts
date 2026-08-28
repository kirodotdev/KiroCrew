/**
 * Shared update contract for the App Store pages (PR2 App Store split).
 *
 * LibraryPage and the Discover Updates sub-page both update the same apps
 * through the same endpoint, so the pieces that define *how* an update
 * behaves — the recorded-source routing, the per-app pending state, and the
 * sequential Update All loop with its progress and failure aggregation —
 * live here, MOVED from LibraryPage (not rewritten). Two inline copies of
 * this plumbing is the drift shape `useAppActions` already exists to prevent.
 *
 * The state is PER HOOK INSTANCE, i.e. per mounted page: what the two
 * surfaces share is the behavior contract, not live progress. Navigating
 * away mid-batch leaves the loop running without visible progress, and the
 * destination page's own instance cannot see it — a known limit, accepted
 * because a cross-page store for a seconds-long operation is not worth its
 * weight (each success still announces, so the destination's data is fresh).
 *
 * Message DISPLAY stays in the pages: the hook reports outcomes through the
 * `setError` / `setSuccess` callbacks it is given, and each page renders (and
 * auto-dismisses) them in its own notice surface.
 */

import { useState } from 'react'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { isRegistrySourced } from '../../components/appstore/types'
import { isTrustDeniedError } from '../../components/appstore/TrustAppModal'
import type { AppsData } from './useAppsData'

type AppUpdatesInput = Pick<AppsData, 'apps' | 'updatables' | 'announceAppsChanged'> & {
  /** Detail-page update navigation from `useAppActions` (registry-sourced apps). */
  updateApp: (name: string) => void
  /** Report a failure into the page's error notice. */
  setError: (msg: string) => void
  /** Show a transient success message (the page owns display + auto-dismiss). */
  setSuccess: (msg: string) => void
  /**
   * Consent hand-off for an in-place update refused with the execution-trust
   * code: the surface opens its trust modal instead of showing raw error
   * text. `retryUpdate` re-runs THE UPDATE after the grant (and rejects on
   * failure, the shape `useTrustGate` expects) — pass it as the gate's retry
   * or consent would resume the gate's default enable action instead. Only
   * consulted on the in-place path; the navigation path lands on the detail
   * page, which owns that presentation itself.
   */
  onTrustDenied?: (name: string, retryUpdate: (name: string) => Promise<void>) => void
  /**
   * Run every per-row update in place instead of routing registry-sourced
   * rows to the detail page. The Updates worklist sets this: its header and
   * Update All establish in-place updating, so a row's Update button
   * navigating away mid-triage is the same word with two behaviors — and the
   * in-place call is exactly the one Update All already makes per row.
   * Library cards leave it unset and keep the recorded-source routing (the
   * detail page owns the streaming log + trust consent presentation there).
   */
  rowUpdatesInPlace?: boolean
}

export function useAppUpdates({
  apps, updatables, announceAppsChanged, updateApp, setError, setSuccess,
  onTrustDenied, rowUpdatesInPlace = false,
}: AppUpdatesInput) {
  /** Sequential Update All progress, or null when no batch is running. */
  const [updatingAll, setUpdatingAll] = useState<{ done: number; total: number } | null>(null)
  /** Name of the app whose single in-place update is in flight, or null. */
  const [updatePending, setUpdatePending] = useState<string | null>(null)

  // Update dispatches on the RECORDED SOURCE, mirroring ``handle_update_app``'s
  // own branch. A registry-sourced app is re-cloned from its registry and the
  // detail page owns that flow (streaming log plus the trust consent modal), so
  // it navigates there — unless `rowUpdatesInPlace` opts the surface into the
  // direct call, which is the same request the detail page and Update All end
  // up making. An app installed from a path has no registry row: it is
  // refreshed in place from the directory recorded at install — the same call
  // Update All makes — and routing it at the registry instead failed every sync
  // with "not found in registry". A row absent from this list carries no source
  // to read, so it navigates and the detail page re-dispatches on the record it
  // loads. Blocked while Update All is running so the same update can't run
  // twice concurrently.
  // The post-consent retry the trust gate runs: the raw update call, success
  // feedback included, REJECTING on failure so the gate can distinguish a
  // landed grant whose retry still failed (`useTrustGate` requires that
  // shape). Not `runUpdate` itself — that catch-all resolves on failure and
  // would make the gate report success over a failed update.
  const retryUpdate = async (name: string) => {
    await api.updateApp(name)
    announceAppsChanged()
    setSuccess(i18nT('pages.appsPage.updated_app', { count: 1 }))
  }

  // One update at a time: `updatePending` is a single slot, so a second
  // in-flight dispatch would clobber the first row's feedback (its button
  // re-enables mid-flight and invites a duplicate request) and the first
  // settle would wipe the second's label. The rows freeze via the same
  // state, so this guard is a backstop, not the primary affordance.
  const runUpdate = async (name: string) => {
    if (updatingAll || updatePending) return
    const target = apps.find(a => a.name === name)
    if (!target || (isRegistrySourced(target) && !rowUpdatesInPlace)) {
      updateApp(name)
      return
    }
    setUpdatePending(name)
    setError('')
    try {
      await api.updateApp(name)
      announceAppsChanged()
      // An in-place sync is the one action here whose success is otherwise
      // INVISIBLE: re-copying a source directory usually carries the same
      // version, so the card re-renders byte-identical and the dev cannot tell
      // whether new bytes landed. Reflect it the way `disable` already does.
      // A registry update's row leaves the worklist on refresh, so it gets the
      // batch path's own success wording instead.
      setSuccess(isRegistrySourced(target)
        ? i18nT('pages.appsPage.updated_app', { count: 1 })
        : i18nT('pages.appsPage.synced_from_its_source_directory', {
            name: target.displayName || name,
          }))
    } catch (e) {
      // A third-party app whose execution trust lapsed is a consent prompt,
      // not an error — hand it to the surface's trust modal when one is
      // wired, mirroring how the enable path branches on the same code.
      if (onTrustDenied && isTrustDeniedError(e)) onTrustDenied(name, retryUpdate)
      else setError((e as Error)?.message || i18nT('pages.appsPage.action_failed', { action: 'update', name }))
    } finally {
      setUpdatePending(null)
    }
  }

  const updateAll = async () => {
    if (updatingAll) return
    const targets = updatables.map(a => a.name)
    setUpdatingAll({ done: 0, total: targets.length })
    setError('')
    const failed: string[] = []
    let succeeded = 0
    for (let i = 0; i < targets.length; i++) {
      try {
        await api.updateApp(targets[i])
        succeeded += 1
      } catch {
        failed.push(targets[i])
      }
      setUpdatingAll({ done: i + 1, total: targets.length })
    }
    setUpdatingAll(null)
    // ONE trailing announce, matching what the Library banner shipped: each
    // announce invalidates ['registry'], and with the app shell holding an
    // always-active observer on that key a per-success announce turns an
    // N-app batch into ~N heavyweight catalog refetches. Mid-batch progress
    // is carried by the {done}/{total} label, not by rows dropping early. A
    // run with zero successes changed nothing worth refreshing.
    if (succeeded > 0) announceAppsChanged()
    if (failed.length) setError(i18nT('pages.appsPage.failed_to_update', { names: failed.join(', ') }))
    else setSuccess(i18nT('pages.appsPage.updated_app', { count: targets.length }))
  }

  return { updatingAll, updatePending, runUpdate, updateAll }
}
