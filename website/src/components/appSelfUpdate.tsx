/**
 * The host side of `useAppUpdate().requestUpdate()`: an installed app asks to be
 * updated, and the host decides with the user.
 *
 * The request is bound to the app the host mounted, so a page can only ever ask
 * about itself. Nothing here widens who may update an app: the call it makes is
 * the App Library's own `POST /api/apps/{name}/update`, under the dashboard
 * owner's session, and only after the user confirms a dialog the app cannot
 * reach into. The two update outcomes that need more consent than "yes, update"
 * are handed to the app's detail page, which already owns them, rather than
 * re-implemented here: execution trust (the detail page re-runs the update
 * behind its trust prompt) and a new version that asks to control chats (the
 * update leaves the app disabled, and the detail page's Enable restates it).
 * New `permissions.api` / `permissions.events` entries go there too: the
 * gateway holds them back until the owner approves them on that page.
 */
import { useCallback, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import type { AppUpdate, AppUpdateResult } from '../app-sdk'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { useConfirm } from './ConfirmDialog'
import ErrorNotice from './ErrorNotice'
import Modal from './Modal'
import { Btn } from './ui'
import { isTrustDeniedError } from './appstore/TrustAppModal'

/** What the host knows about the app it mounted. */
export interface SelfUpdateTarget {
  name: string
  /** What the user calls the app, for the dialog copy. */
  label: string
  /** The installed version the page was loaded at. */
  version: string
  update?: AppUpdate
  lifecycle?: string
}

/** The installed record's fields this flow reads back after an update. */
interface InstalledRecord {
  version?: string
  approvedGrants?: { api?: string[]; events?: string[] }
  manifest?: { permissions?: { api?: string[]; events?: string[] } }
}

/** Whether the update left declared api/events entries awaiting approval. The
 *  gateway records `approvedGrants` only while some are held back; this is the
 *  same comparison the details page draws its approval block from. */
function hasStagedGrants(record: InstalledRecord | null): boolean {
  const approved = record?.approvedGrants
  if (!approved) return false
  const declared = record?.manifest?.permissions
  return (declared?.api || []).some(p => !(approved.api || []).includes(p))
    || (declared?.events || []).some(e => !(approved.events || []).includes(e))
}

/** The gateway's machine-readable failure code, read off a rejected request. */
function failureCode(e: unknown): string {
  if (e && typeof e === 'object') {
    const direct = (e as { code?: unknown }).code
    if (typeof direct === 'string' && direct) return direct
    const body = (e as { body?: unknown }).body
    if (typeof body === 'string' && body.trim().startsWith('{')) {
      try {
        const code = (JSON.parse(body) as { code?: unknown }).code
        if (typeof code === 'string' && code) return code
      } catch { /* not JSON: no code */ }
    }
  }
  return 'update_failed'
}

/**
 * Whether the page has a fresh user gesture to show a dialog on. Browsers that
 * do not expose `userActivation` get the benefit of the doubt: the dialog still
 * needs a click to do anything, so the gesture check only stops an app from
 * raising it unprompted where the browser can tell.
 */
function hasUserGesture(): boolean {
  const activation = (navigator as Navigator & { userActivation?: { isActive: boolean } }).userActivation
  return activation ? activation.isActive : true
}

/**
 * Returns the bound `requestUpdate` the host hands to `AppApiProvider`, and the
 * dialog element the host must render. `active` is the surface's visibility: a
 * hidden side-panel tab may not raise a dialog over whatever the user is doing.
 */
export function useAppSelfUpdate(target: SelfUpdateTarget, active: boolean): {
  requestUpdate: () => Promise<AppUpdateResult>
  dialog: ReactNode
} {
  const navigate = useNavigate()
  const { confirm, confirmDialog } = useConfirm()
  // Read at call time, not captured: the host re-renders the provider with a
  // fresh `update` answer, and a request must judge the newest one.
  const targetRef = useRef(target)
  targetRef.current = target
  const activeRef = useRef(active)
  activeRef.current = active
  const busy = useRef(false)
  // What this hook installed, for a host whose record is not re-read after an
  // update (the routed page loads it once). Without it a declined reload leaves
  // the old version and a stale `update.available`, and the next request offers
  // the same release again.
  const installedRef = useRef<string | null>(null)
  const [updatingLabel, setUpdatingLabel] = useState('')
  const [updateFailure, setUpdateFailureState] = useState<{ label: string; reason: string } | null>(null)
  // Read by the guard below. State alone is not enough: an app re-calling the
  // `requestUpdate` it captured before the failure would see the stale value.
  const failureOpen = useRef(false)
  const setUpdateFailure = useCallback((f: { label: string; reason: string } | null) => {
    failureOpen.current = f !== null
    setUpdateFailureState(f)
  }, [])

  // Held through the reload offer as well: a second request would answer that
  // offer "no" (`useConfirm` resolves a superseded prompt false) and run the
  // same update again.
  const offerReload = useCallback(async (label: string, version: string) => {
    const reload = await confirm({
      title: i18nT('components.appSelfUpdate.reload_title', { version }),
      body: i18nT('components.appSelfUpdate.reload_body', { name: label }),
      confirmLabel: i18nT('components.appSelfUpdate.reload_now'),
      primary: true,
    })
    if (reload) window.location.reload()
  }, [confirm])

  const requestUpdate = useCallback(async (): Promise<AppUpdateResult> => {
    const { name, label, update, lifecycle } = targetRef.current
    const version = installedRef.current ?? targetRef.current.version
    const fail = (error: string, message?: string): AppUpdateResult =>
      ({ status: 'failed', version, error, ...(message ? { message } : {}) })
    // Every refusal before the dialog is silent to the user: the app asked for
    // something it may not have, and it gets the reason back to handle itself.
    if (!activeRef.current) return fail('inactive')
    if (!hasUserGesture()) return fail('no_user_gesture')
    if ((lifecycle ?? 'gateway') !== 'gateway') return fail('lifecycle_not_gateway')
    if (!update?.available || installedRef.current === update.latestVersion) return fail('no_update_available')
    if (busy.current || failureOpen.current) return fail('busy')
    busy.current = true
    let releaseLater = false
    try {
      const ok = await confirm({
        title: i18nT('components.appSelfUpdate.ask_title', { name: label }),
        body: i18nT('components.appSelfUpdate.ask_body', {
          name: label, installed: version, version: update.latestVersion,
        }),
        confirmLabel: i18nT('components.appSelfUpdate.update_now'),
        primary: true,
        // The app shares this document, so it could click the button itself.
        requireTrustedPress: true,
      })
      if (!ok) return { status: 'cancelled', version }

      let notice: string | undefined
      // Keep host-owned feedback visible while the registry update runs. An app
      // cannot forge or dismiss this state through the SDK notification bridge.
      setUpdatingLabel(i18nT('components.appSelfUpdate.updating', { name: label }))
      try {
        notice = (await api.updateApp(name) as { notice?: string } | undefined)?.notice
      } catch (e) {
        setUpdatingLabel('')
        if (isTrustDeniedError(e)) {
          // The detail page's update path owns the trust prompt and retries the
          // update behind it, the same hand-off the App Library makes.
          navigate(`/apps/detail/${encodeURIComponent(name)}`, { state: { autoAction: 'update' } })
          return fail('app_execution_denied')
        }
        const message = e instanceof Error ? e.message : ''
        const code = failureCode(e)
        setUpdateFailure({ label, reason: message || code })
        return fail(code, message)
      }
      window.dispatchEvent(new Event('mc:apps-changed'))
      installedRef.current = update.latestVersion

      // The installed record is the authority on what landed. If that read
      // fails, route to the details page rather than offering a reload while
      // the new version's permission state is unknown.
      let installed = update.latestVersion
      let fresh: InstalledRecord | null = null
      try {
        fresh = await api.getApp(name) as InstalledRecord | null
        if (fresh?.version) installed = fresh.version
        installedRef.current = installed
      } catch {
        setUpdatingLabel('')
        navigate(`/apps/detail/${encodeURIComponent(name)}`)
        return {
          status: 'updated',
          version: installed,
          ...(notice ? { notice } : {}),
        }
      }

      if (notice === 'session_approval_reconsent') {
        setUpdatingLabel('')
        navigate(`/apps/detail/${encodeURIComponent(name)}`)
        return { status: 'updated', version: installed, notice }
      }
      if (hasStagedGrants(fresh)) {
        // The new version runs without its new permissions until the owner
        // approves them, and the details page is where that approval lives.
        setUpdatingLabel('')
        navigate(`/apps/detail/${encodeURIComponent(name)}`)
        return { status: 'updated', version: installed, notice: 'grants_staged' }
      }
      // After the result is in the app's hands: the old bundle may want to
      // save state before the user reloads it away.
      setUpdatingLabel('')
      releaseLater = true
      void offerReload(label, installed).finally(() => { busy.current = false })
      return { status: 'updated', version: installed }
    } finally {
      if (!releaseLater) busy.current = false
    }
  }, [confirm, navigate, offerReload, setUpdateFailure])

  const feedbackDialog = (
    <>
      <Modal
        open={!!updatingLabel}
        onClose={() => {}}
        title={updatingLabel}
        maxWidth={440}
        dismissDisabled
      >
        <p role="status" className="text-sm text-text m-0">{updatingLabel}</p>
      </Modal>
      <Modal
        open={!!updateFailure}
        onClose={() => setUpdateFailure(null)}
        title={i18nT('components.appSelfUpdate.failure_title')}
        maxWidth={440}
        footer={
          <Btn primary onClick={() => setUpdateFailure(null)}>
            {i18nT('components.errorNotice.dismiss')}
          </Btn>
        }
      >
        <ErrorNotice
          askAgent
          title={i18nT('components.appSelfUpdate.failure_summary', {
            name: updateFailure?.label ?? '',
          })}
          message={updateFailure?.reason}
          messagePlacement="below"
        />
      </Modal>
    </>
  )

  return { requestUpdate, dialog: <>{confirmDialog}{feedbackDialog}</> }
}
