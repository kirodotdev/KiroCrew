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
 * `setError` / `setNotice` / `setSuccess` callbacks it is given, and each page
 * renders them in its own notice surface (only success auto-dismisses).
 */

import { useState } from 'react'
import { api } from '../../api/client'
import { fmtList } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { isMarketSourced, isRegistrySourced } from '../../components/appstore/types'
import { isTrustDeniedError, localOnlyTrustRepository } from '../../components/appstore/TrustAppModal'
import { backendRefusedNotice } from '../../components/appstore/backendRefused'
import type { AppsData } from './useAppsData'

type AppUpdatesInput = Pick<AppsData, 'apps' | 'updatables' | 'announceAppsChanged'> & {
  /** Detail-page update navigation from `useAppActions` (registry-sourced apps). */
  updateApp: (name: string) => void
  /** Report a failure into the page's error notice. */
  setError: (msg: string) => void
  /** Show a transient success message (the page owns display + auto-dismiss). */
  setSuccess: (msg: string) => void
  /**
   * Report Update All's chat-control consent lines (the update landed but left
   * the app disabled until the user consents) into a non-error notice that
   * stays until dismissed, so the error notice carries only what failed or was
   * refused. `''` clears it, as each new update does. A surface without such a
   * notice leaves this unset and the lines join its error notice, so none is
   * ever dropped.
   */
  setNotice?: (msg: string) => void
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

/** One notice's text and the apps it sends to their details (see `trustHandoff`). */
type TrustHandoff = { message: string; apps: { name: string; label: string; rerunUpdate: boolean }[] }

export function useAppUpdates({
  apps, updatables, announceAppsChanged, updateApp, setError, setSuccess,
  setNotice = setError, onTrustDenied, rowUpdatesInPlace = false,
}: AppUpdatesInput) {
  /** Sequential Update All progress, or null when no batch is running. */
  const [updatingAll, setUpdatingAll] = useState<{ done: number; total: number } | null>(null)
  /** Name of the app whose single in-place update is in flight, or null. */
  const [updatePending, setUpdatePending] = useState<string | null>(null)
  /**
   * The last Update All notices that sent apps to their details (repository
   * trust, or chat-control consent), each with those apps, so the page can
   * link each one from the notice that names it. `rerunUpdate` is true for a
   * repository-trust app, whose detail page reruns the update and offers that
   * trust; a consent app's update already landed, so its link only opens the
   * page. Keyed on the notice text: the page shows an entry's links only in
   * the notice that is showing that exact text.
   */
  const [trustHandoff, setTrustHandoff] = useState<TrustHandoff[]>([])
  const label = (name: string) => apps.find(a => a.name === name)?.displayName || name
  /** Clear both notices before a new update reports, so no earlier line outlives it. */
  const clearNotices = () => {
    setError('')
    if (setNotice !== setError) setNotice('')
  }
  /**
   * The `source` override for an update of *name*: the registry for a path
   * install that a same-named registry row offers a newer version of (it is in
   * `updatables`, so its tile and Updates row say Update, not Sync), so the
   * call installs that version instead of re-copying the folder. Nothing
   * otherwise: a registry-sourced app's recorded source already is the
   * registry, and a path install with no update pending is a Sync.
   */
  const registrySourceFor = (name: string) => {
    const target = apps.find(a => a.name === name)
    const pending = updatables.some(a => a.name === name)
    return target && pending && !isRegistrySourced(target) && !isMarketSourced(target) ? `registry:${name}` : undefined
  }
  /** One update call, with the registry override when `registrySourceFor` gives one. */
  const callUpdate = (name: string) => {
    const source = registrySourceFor(name)
    return source ? api.updateApp(name, source) : api.updateApp(name)
  }

  // Update dispatches on the RECORDED SOURCE, mirroring ``handle_update_app``'s
  // own branch. A registry-sourced app is re-cloned from its registry and the
  // detail page owns that flow (streaming log plus the trust consent modal), so
  // it navigates there — unless `rowUpdatesInPlace` opts the surface into the
  // direct call, which is the same request the detail page and Update All end
  // up making. An app installed from a path is refreshed in place from the
  // directory recorded at install (the Sync verb), the same call Update All
  // makes for it otherwise, and routing it at the registry instead failed every
  // sync with "not found in registry". A path install that a registry row offers
  // a newer version of is an Update, which installs that version, as the detail
  // page's Update button does: it routes like a registry-sourced app, and the
  // in-place call sends the registry source (`registrySourceFor`). A row absent
  // from this list carries no source to read, so it navigates and the detail
  // page re-dispatches on the record it loads. Blocked while Update All is
  // running so the same update can't run twice concurrently.
  // The post-consent retry the trust gate runs: the raw update call, success
  // feedback included, REJECTING on failure so the gate can distinguish a
  // landed grant whose retry still failed (`useTrustGate` requires that
  // shape). Not `runUpdate` itself — that catch-all resolves on failure and
  // would make the gate report success over a failed update.
  const retryUpdate = async (name: string) => {
    let updateResult: Awaited<ReturnType<typeof callUpdate>>
    try {
      updateResult = await callUpdate(name)
    } catch (e) {
      // The grant just saved covers the app's local folder only, which the
      // registry update does not install from: the detail page asks to trust
      // the repository, as `runUpdate` hands off. The trust step itself worked.
      if (!localOnlyTrustRepository(e)) throw e
      announceAppsChanged()
      updateApp(name)
      return
    }
    announceAppsChanged()
    if (updateResult?.notice === 'session_approval_reconsent') {
      updateApp(name)
      return
    }
    const refused = backendRefusedNotice(label(name), updateResult)
    if (refused) setError(refused)
    else setSuccess(i18nT('pages.appsPage.updated_app', { count: 1 }))
  }

  // One update at a time: `updatePending` is a single slot, so a second
  // in-flight dispatch would clobber the first row's feedback (its button
  // re-enables mid-flight and invites a duplicate request) and the first
  // settle would wipe the second's label. The rows freeze via the same
  // state, so this guard is a backstop, not the primary affordance.
  const runUpdate = async (name: string) => {
    if (updatingAll || updatePending) return
    const target = apps.find(a => a.name === name)
    // A Kiro Space install has no Sync or Update yet; the gateway refuses both.
    if (target && isMarketSourced(target)) return
    const fromRegistry = !!target && (isRegistrySourced(target) || !!registrySourceFor(name))
    if (!target || (fromRegistry && !rowUpdatesInPlace)) {
      updateApp(name)
      return
    }
    setUpdatePending(name)
    clearNotices()
    try {
      const updateResult = await callUpdate(name)
      announceAppsChanged()
      if (updateResult?.notice === 'session_approval_reconsent') {
        updateApp(name)
        return
      }
      // An in-place sync is the one action here whose success is otherwise
      // INVISIBLE: re-copying a source directory usually carries the same
      // version, so the card re-renders byte-identical and the dev cannot tell
      // whether new bytes landed. Reflect it the way `disable` already does.
      // A registry update's row leaves the worklist on refresh, so it gets the
      // batch path's own success wording instead. A backend the host refused
      // is a warning, not a success: the update landed but nothing runs.
      const refused = backendRefusedNotice(target.displayName || name, updateResult)
      if (refused) setError(refused)
      else setSuccess(fromRegistry
        ? i18nT('pages.appsPage.updated_app', { count: 1 })
        : i18nT('pages.appsPage.synced_from_its_source_directory', {
            name: target.displayName || name,
          }))
    } catch (e) {
      // A grant that covers only the app's local folder: the detail page is the
      // surface that offers to trust the repository, so the row hands off there,
      // as it does for an update that needs new chat-control consent.
      if (localOnlyTrustRepository(e)) updateApp(name)
      // A third-party app whose execution trust lapsed is a consent prompt,
      // not an error — hand it to the surface's trust modal when one is
      // wired, mirroring how the enable path branches on the same code.
      else if (onTrustDenied && isTrustDeniedError(e)) onTrustDenied(name, retryUpdate)
      else setError((e as Error)?.message || i18nT('pages.appsPage.failed_to_update', { names: name }))
    } finally {
      setUpdatePending(null)
    }
  }

  const updateAll = async () => {
    if (updatingAll) return
    const targets = updatables
      .filter(a => { const target = apps.find(x => x.name === a.name); return !(target && isMarketSourced(target)) })
      .map(a => a.name)
    setUpdatingAll({ done: 0, total: targets.length })
    clearNotices()
    setTrustHandoff([])
    const failed: string[] = []
    const needsTrust: string[] = []
    let succeeded = 0
    const consentNeeded: string[] = []
    const trustNeeded: string[] = []
    const refusedNotices: string[] = []
    for (let i = 0; i < targets.length; i++) {
      try {
        const updateResult = await callUpdate(targets[i])
        succeeded += 1
        if (updateResult?.notice === 'session_approval_reconsent') {
          consentNeeded.push(targets[i])
        }
        const refused = backendRefusedNotice(label(targets[i]), updateResult)
        if (refused) refusedNotices.push(refused)
      } catch (e) {
        // A grant that covers only the app's local folder is a refusal with a
        // next step, not a bare failure: the detail page offers to trust the
        // repository. The batch keeps going and names the app afterwards
        // instead of navigating mid-run.
        if (localOnlyTrustRepository(e)) trustNeeded.push(targets[i])
        // A third-party app whose execution trust lapsed is not a failure with
        // no reason — the batch cannot open a trust modal per row, so name it
        // as needing trust (the per-row Update opens the dialog), mirroring the
        // branch `runUpdate` already takes. Anything else is a real failure.
        else if (isTrustDeniedError(e)) needsTrust.push(targets[i])
        else failed.push(targets[i])
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
    // Two notices for the whole batch, and the user hears about every app in
    // them, not just the first. A refused update (the app needs permission, or
    // needs its repository trusted) and a refused backend are errors by where
    // they come from (`errors-use-error-notice`), so they join the failures.
    // An update that newly asks for chat control landed but left the app
    // DISABLED pending consent: nothing failed, so that line is not an error.
    const errors: string[] = []
    if (failed.length) errors.push(i18nT('pages.appsPage.failed_to_update', { names: fmtList(failed.map(label), { style: 'narrow' }) }))
    for (const name of needsTrust) {
      errors.push(i18nT('pages.appsPage.update_needs_trust', { name: label(name) }))
    }
    for (const name of trustNeeded) {
      errors.push(i18nT('pages.appsPage.update_needs_repository_trust', { name: label(name) }))
    }
    errors.push(...refusedNotices)
    const consent = consentNeeded.map(name =>
      i18nT('pages.appsPage.updated_needs_session_approval_consent', { name: label(name) }))
    // One line per fact: both notice surfaces are whitespace-pre-wrap, so a
    // newline keeps "Docs failed" and "Crew Keyboard awaits consent" from
    // reading as one sentence about each other. A surface with no separate
    // notice gets every line in its error notice, failures first. A notice
    // that names a repository-trust or consent app carries those apps, so the
    // page offers each one's details from the notice that names it.
    const trustApps = trustNeeded.map(name => ({ name, label: label(name), rerunUpdate: true }))
    const consentApps = consentNeeded.map(name => ({ name, label: label(name), rerunUpdate: false }))
    const notices = setNotice === setError
      ? [{ show: setError, lines: [...errors, ...consent], links: [...trustApps, ...consentApps] }]
      : [{ show: setError, lines: errors, links: trustApps }, { show: setNotice, lines: consent, links: consentApps }]
    const handoffs: TrustHandoff[] = []
    for (const { show, lines, links } of notices) {
      if (!lines.length) continue
      const message = lines.join('\n')
      show(message)
      if (links.length) handoffs.push({ message, apps: links })
    }
    setTrustHandoff(handoffs)
    if (!errors.length && !consent.length) {
      setSuccess(i18nT('pages.appsPage.updated_app', { count: targets.length }))
    }
  }

  return { updatingAll, updatePending, runUpdate, updateAll, trustHandoff }
}
