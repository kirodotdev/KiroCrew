/**
 * Crew deep links (`kirocrew://chat?crew=<crew id>&sid=<key>`), the dashboard
 * half; the behaviour is specified in docs/system-specs/modules/instances.md,
 * "Crew deep link".
 *
 *  - Hub: `useCrewDeepLink` handles the route the desktop resolver hands over
 *    the native navigate channel (never read from the router URL), and
 *    `useDeliverPendingPaneSlot` posts the held key to the crew's pane.
 *  - Pane: `usePaneOpenSlot` opens a posted key the way a local session link does.
 */
import { useCallback, useEffect, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, type InstanceView } from '../api/client'
import { useAppDispatch, useAppSelector, useAppStore } from '../store'
import { setActiveId, setPendingPaneSlot, type WarmConn } from '../store/instancesSlice'
import { setAgentSwitchNotice, switchSlot } from '../store/chatSlice'
import { i18nT } from '../i18n/t'
import { useSelectInstance } from '../hooks/useSelectInstance'
import { autoConnectEnabled } from '../hooks/useAutoConnectInstances'
import { connectInstanceInto } from './connectInstance'
import { isEmbeddedPane } from './embedded'
import { paneOriginFor } from './paneCache'
import { rememberPaneSlot } from './paneLastSlot'
import { rememberActivePane } from './activePaneMemory'
import { hasDashboardPane } from '../utils/remoteCrew'
import { applyNavIntentInMain } from '../utils/navIntent'

/** Hub -> pane: open this session. Carries only the key. */
export const PANE_OPEN_SLOT_TYPE = 'mc-open-slot'

/** The resolver's crew-link key rule (`CREW_SLOT_KEY_RE` in deep-link.js). */
export function isCrewLinkKey(v: unknown): v is string {
  return typeof v === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(v)
}

export type CrewDeepLink = { kind: 'error' } | { kind: 'open'; crewId: string; key: string }

/** Read a crew link off a native navigate path, or null when it is not one. */
export function parseCrewDeepLink(path: string): CrewDeepLink | null {
  let url: URL
  try {
    url = new URL(path, 'http://dashboard.invalid')
  } catch {
    return null
  }
  if (url.pathname !== '/chat') return null
  const params = url.searchParams
  if (params.get('crewError') === '1') return { kind: 'error' }
  if (!params.has('crew')) return null
  const crewId = params.get('crew') || ''
  const key = params.get('crewSid')
  if (!crewId || !isCrewLinkKey(key)) return { kind: 'error' }
  return { kind: 'open', crewId, key }
}

type Nav = { navigate: (path: string) => void; mayLeave: () => boolean }

const NO_INSTANCES: InstanceView[] = []

/**
 * Hub side. Returns the native-navigate handler: true when `path` was a crew
 * link (handled here, refused or not), false to let the caller route it.
 */
export function useCrewDeepLink(): (path: string) => boolean {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const queryClient = useQueryClient()
  // The link always hands the select the row it just read, so no cached list.
  const { selectInstance } = useSelectInstance(NO_INSTANCES)
  // Read after an await, so it must be the latest render's.
  const selectRef = useRef(selectInstance)
  selectRef.current = selectInstance
  // The newest native link wins: a crew link still awaiting its crew list gives
  // up when any later link arrives, or when the user switched panes meanwhile.
  const seq = useRef(0)

  return useCallback((path: string) => {
    const mine = ++seq.current
    const link = parseCrewDeepLink(path)
    if (!link) return false
    // A newer crew link drops a key an older one still holds for a pane not ready yet.
    if (store.getState().instances?.pendingPaneSlot) dispatch(setPendingPaneSlot(null))
    const activeAtArrival = store.getState().instances?.activeId ?? null
    // Latched: a switch away and back to the arrival pane still gives up.
    let moved = false
    const superseded = () => mine !== seq.current || moved || (store.getState().instances?.activeId ?? null) !== activeAtArrival
    // The shell's error banner, drawn over every local page: the link moves
    // nothing but the local crew forward (where the banner is drawn).
    const refuse = (message: string) => {
      if (superseded()) return
      if (store.getState().instances?.activeId != null) dispatch(setActiveId(null))
      dispatch(setAgentSwitchNotice({ message, error: true }))
    }
    // A reload restores its remembered pane on the first crew list; a link that
    // arrives first, even a malformed one, is the pick that restore yields to
    // (InstancesViewport), so its error is not hidden behind the restored crew.
    if (activeAtArrival === null) rememberActivePane(null)
    if (link.kind === 'error') {
      refuse(i18nT('store.chatSlice.crew_link_malformed'))
      return true
    }
    const unsubscribe = store.subscribe(() => {
      if ((store.getState().instances?.activeId ?? null) !== activeAtArrival) moved = true
    })
    void (async () => {
      let instances: InstanceView[]
      try {
        // A fresh read under its own key: the cached ['instances'] can still say
        // `connected` for a tunnel that died while the window was hidden, and
        // the window raise's ['instances'] invalidations would cancel a read
        // in flight there. Keeps the shared retry policy.
        instances = (await queryClient.fetchQuery({
          queryKey: ['crew-deep-link', mine], queryFn: () => api.listInstances(), staleTime: 0, gcTime: 0,
        })).instances
      } catch {
        refuse(i18nT('store.chatSlice.crew_link_load_failed'))
        return
      }
      const inst = instances.find(i => i.id === link.crewId)
      if (!inst) {
        refuse(i18nT('store.chatSlice.crew_link_unknown'))
        return
      }
      if (inst.disabled) {
        refuse(i18nT('store.chatSlice.crew_link_off'))
        return
      }
      if (!hasDashboardPane(inst)) {
        refuse(i18nT('store.chatSlice.crew_link_no_dashboard'))
        return
      }
      if (superseded()) return
      const autoConnect = autoConnectEnabled()
      if (!autoConnect) {
        const st = await connectInstanceInto(dispatch, inst.id, 'link', { onlyIfConnected: true }).catch(() => null)
        if (st?.state !== 'connected' || !st.local_port || !st.token) {
          refuse(i18nT('store.chatSlice.crew_link_not_connected'))
          return
        }
      }
      if (superseded()) return
      const w = store.getState().instances.warm[inst.id]
      let reloads = false
      if (autoConnect) reloads = selectRef.current(inst.id, inst)
      else dispatch(setActiveId(inst.id))
      // A pane the select is (re)loading picks the chat up from its last-chat
      // hint (the src is built once the connect answers), which is how a remote
      // crew on an older build still opens it. A live pane gets only the
      // message, so its own hint stays its own.
      if (reloads) rememberPaneSlot(inst.id, link.key)
      // A pane whose tunnel dropped still reads ready for its old token; the
      // reconnect mints a new one and reloads it, so wait for that.
      const staleToken = reloads && w ? w.token : undefined
      dispatch(setPendingPaneSlot({ id: inst.id, key: link.key, staleToken }))
    })().finally(unsubscribe)
    return true
  }, [dispatch, store, queryClient])
}

/** Hub side: post the held key once its crew's pane is ready for a src loaded after the link. */
export function useDeliverPendingPaneSlot(
  frames: { current: Map<string, HTMLIFrameElement> },
  warm: Record<string, WarmConn>,
  ready: Record<string, boolean>,
) {
  const dispatch = useAppDispatch()
  const pending = useAppSelector(s => s.instances.pendingPaneSlot)
  useEffect(() => {
    if (!pending || !ready[pending.id]) return
    const w = warm[pending.id]
    const target = frames.current.get(pending.id)?.contentWindow
    if (!w || w.token === pending.staleToken || !target) return
    try {
      // The pane's exact loopback origin, never '*', like the host model.
      target.postMessage({ type: PANE_OPEN_SLOT_TYPE, v: 1, key: pending.key }, paneOriginFor(w.port))
    } catch {
      return // frame mid-navigation: its next ready retries
    }
    dispatch(setPendingPaneSlot(null))
  }, [pending, ready, warm, frames, dispatch])
}

/** Pane side: open a key the hub posted, from whatever page is on screen. */
export function usePaneOpenSlot(nav: Nav) {
  const dispatch = useAppDispatch()
  const navRef = useRef(nav)
  navRef.current = nav
  useEffect(() => {
    if (!isEmbeddedPane()) return
    const onMessage = (e: MessageEvent) => {
      // Only our direct parent; its CSP-pinned origin is the hub.
      if (e.source !== window.parent) return
      const d = e.data as { type?: unknown; v?: unknown; key?: unknown } | null
      if (d?.type !== PANE_OPEN_SLOT_TYPE || d.v !== 1 || !isCrewLinkKey(d.key)) return
      applyNavIntentInMain(
        { path: '/chat', slotKey: d.key },
        {
          navigate: navRef.current.navigate,
          switchSlot: key => { dispatch(switchSlot({ key, announceOnMissing: true })) },
          mayLeave: navRef.current.mayLeave,
        },
      )
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [dispatch])
}
