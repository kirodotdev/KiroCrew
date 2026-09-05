/**
 * EmbeddedHostBridge — the embedded (remote-pane) half of the
 * consolidated-header relay.
 *
 * When this dashboard SPA runs inside an InstancesViewport <iframe> it has no
 * knowledge of the parent's instance list, which pane is active, or whether the
 * parent is a macOS Electron window whose native traffic lights overlap the top
 * of the pane. The parent relays all of that down as a single `mc-host-model`
 * postMessage; this bridge stores it in `instances.host` so the embedded header
 * can render the instance switcher inline (EmbeddedInstanceTabBar) and the
 * readout capsule can show the tunnel refresh countdown — collapsing
 * the remote pane's two bars into one.
 *
 * Security: we only trust messages whose `event.source` is our direct parent
 * (`window.parent`). The parent independently validates the loopback ORIGIN of
 * our switch requests (see InstancesViewport / tunnelOrigin), so trust is gated
 * on both ends. Non-embedded (top-level) dashboards mount this as a no-op.
 */
import { useEffect } from 'react'
import { useAppDispatch } from '../store'
import { setHostModel, type HostModel } from '../store/instancesSlice'
import { isEmbeddedPane } from '../lib/embedded'
import { setFocusModeEnabled } from '../hooks/useFocusMode'

const MAC_INSET_CLASS = 'embedded-mac-inset'

// Backoff (ms) between re-announcements of `mc-embedded-ready`, applied AFTER
// the initial announce. The handshake is: child posts `mc-embedded-ready`, the
// parent marks the pane ready (lifting its loading overlay) AND answers with a
// distinct `mc-embedded-ack`. A single announce loses that race whenever the
// parent's message listener / origin map isn't wired when it fires — a first
// mount, or a reload — and the pane then loads but strands the parent on
// "loading" forever. Re-announcing on a bounded schedule closes that race; the
// ack is what stops it.
//
// Deliberately BOUNDED, not an infinite loop: the schedule is capped and its
// total (~7.75s of cumulative delay) stays comfortably under the parent's
// PANE_LOAD_TIMEOUT_MS (15s). So a pane the parent genuinely never answers stops
// re-posting and falls through to the parent's error panel (which carries Retry)
// instead of spinning — the cure and the give-up are both finite.
const HANDSHAKE_RETRY_DELAYS_MS = [250, 500, 1000, 2000, 4000]

/** Narrow an untrusted payload to a HostModel, dropping anything malformed. */
function parseHostModel(data: unknown): HostModel | null {
  if (!data || typeof data !== 'object') return null
  const d = data as Record<string, unknown>
  if (d.type !== 'mc-host-model') return null
  const rawTabs = Array.isArray(d.tabs) ? d.tabs : []
  const tabs = rawTabs
    .filter((t): t is Record<string, unknown> => !!t && typeof t === 'object')
    .map(t => ({
      id: String(t.id ?? ''),
      name: String(t.name ?? ''),
      sshHost: String(t.sshHost ?? ''),
      state: typeof t.state === 'string' ? t.state : undefined,
      unread: Number(t.unread) || 0,
    }))
    .filter(t => t.id)
  const rawSelf = d.self && typeof d.self === 'object' ? (d.self as Record<string, unknown>) : null
  const self = rawSelf
    ? {
        state: typeof rawSelf.state === 'string' ? rawSelf.state : undefined,
        ttlRemaining: typeof rawSelf.ttlRemaining === 'number' ? rawSelf.ttlRemaining : undefined,
        ttlTotal: typeof rawSelf.ttlTotal === 'number' ? rawSelf.ttlTotal : undefined,
      }
    : null
  return {
    tabs,
    activeId: typeof d.activeId === 'string' ? d.activeId : null,
    self,
    macInset: !!d.macInset,
    // Tri-state on purpose: `false` and "the host never sent the field" must
    // not collapse. An older host omits it AND ignores the pane's echoed
    // `mc-set-focus-mode`, so coercing absence to `false` would revert a
    // user-driven pane toggle on every host-model re-broadcast.
    focusMode: typeof d.focusMode === 'boolean' ? d.focusMode : null,
    electron: !!d.electron,
    // Element-wise validation, not a blind cast: this crosses a postMessage
    // boundary, so a malformed or hostile payload must degrade to "nothing
    // pinned" rather than putting non-strings into the pin set.
    pinnedCrews: Array.isArray(d.pinnedCrews)
      ? d.pinnedCrews.filter((id): id is string => typeof id === 'string')
      : [],
    // Tri-state, mirroring `focusMode` above: a non-boolean (typically absent)
    // means the host predates this relay, so it has no `mc-set-stable-order`
    // handler either. `null` records "no opinion" so the bar can order by the
    // pre-relay default AND withhold a toggle that host could never honor;
    // coercing to `false` would make the two cases indistinguishable.
    stableOrder: typeof d.stableOrder === 'boolean' ? d.stableOrder : null,
  }
}

export default function EmbeddedHostBridge() {
  const dispatch = useAppDispatch()

  useEffect(() => {
    if (!isEmbeddedPane()) return

    // Stop re-announcing ONLY on the parent's distinct `mc-embedded-ack`, not on
    // a host model. The parent broadcasts its model to every warm pane on any
    // input change (InstancesViewport's broadcast effect), independent of the
    // readiness handshake — so a received model is NOT proof the parent recorded
    // our readiness, and treating it as an ack would (a) let a spontaneous
    // broadcast that raced past a dropped announce stop the retries prematurely,
    // and (b) worse, let a late announce re-mark readiness after the parent gave
    // the pane up on auth-budget exhaustion, suppressing its Retry panel. The ack
    // is sent only from the parent's ready handler, after readiness is recorded,
    // so once it lands there is no pending announce left to revive readiness.
    let acked = false
    const retryTimers: number[] = []

    const announceReady = () => {
      try {
        // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
        window.parent?.postMessage({ type: 'mc-embedded-ready', v: 1 }, '*')
      } catch {
        /* no parent / cross-origin restriction — a later retry covers it */
      }
    }

    const onMessage = (e: MessageEvent) => {
      // Only trust our direct parent — origin is validated on the parent side.
      if (e.source !== window.parent) return
      // The distinct readiness ack: the parent has recorded our readiness, so
      // stop re-announcing. This is the ONLY signal that cancels the retries.
      if ((e.data as { type?: unknown })?.type === 'mc-embedded-ack') {
        if (!acked) {
          acked = true
          for (const t of retryTimers) window.clearTimeout(t)
          retryTimers.length = 0
        }
        return
      }
      const model = parseHostModel(e.data)
      if (!model) return
      dispatch(setHostModel(model))
      document.documentElement.classList.toggle(MAC_INSET_CLASS, model.macInset)
      // Adopt the host window's focus mode. `echo: false` because this IS the
      // relayed value — sending it back up is what would make the two frames
      // ping-pong. A toggle the user drives inside this pane still echoes.
      // `null` = the host sent no opinion (older host): keep the pane's state.
      if (model.focusMode !== null) setFocusModeEnabled(model.focusMode, { echo: false })
    }
    window.addEventListener('message', onMessage)

    // Announce readiness now, then re-announce on a bounded backoff until the
    // parent's `mc-embedded-ack` arrives — closes the mount/reload race where the
    // parent's listener wasn't wired for the first announce. Each retry no-ops
    // once acked, and the whole schedule is finite (see HANDSHAKE_RETRY_DELAYS_MS):
    // a parent that never acks lets the pane fall through to the parent's error
    // panel rather than re-posting forever.
    announceReady()
    let elapsed = 0
    for (const delay of HANDSHAKE_RETRY_DELAYS_MS) {
      elapsed += delay
      retryTimers.push(window.setTimeout(() => { if (!acked) announceReady() }, elapsed))
    }

    return () => {
      window.removeEventListener('message', onMessage)
      for (const t of retryTimers) window.clearTimeout(t)
      document.documentElement.classList.remove(MAC_INSET_CLASS)
      setFocusModeEnabled(false, { echo: false })
      dispatch(setHostModel(null))
    }
  }, [dispatch])

  return null
}
