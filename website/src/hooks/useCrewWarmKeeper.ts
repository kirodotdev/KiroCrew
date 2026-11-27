/**
 * useCrewWarmKeeper — the one owner of the warm set's effects: it records which
 * Crew the person opened (the order useCrewWarmSet ranks by), prewarms the relay
 * Crews in the set, and drops a warm pane once its Crew falls out of it.
 *
 * Run by useAutoConnectInstances, so it is registered once with it from the app
 * shell; it does nothing inside an embedded pane. `prewarm` is the Auto-connect
 * crews switch, read per pass.
 */
import { useEffect, useRef, useState } from 'react'
import { useAppDispatch, useAppSelector } from '../store'
import { relayPaneOpened, removeWarm, type WarmConn } from '../store/instancesSlice'
import { isEmbeddedPane } from '../lib/embedded'
import { relayPaneBridge, relayPaneRect } from '../lib/relayCrewPanes'
import { noteCrewOpened, recentCrews, relayCrewKey, sshCrewKey } from '../lib/crewWarmSet'
import { isRelayCrewOnline, useRelayViewerCrews } from './useRelayViewerCrews'
import { useCrewWarmSet } from './useCrewWarmSet'

const EMPTY_WARM: Record<string, WarmConn> = {}
const NO_IDS: string[] = []
/** A relay prewarm that failed is not tried again for this long. */
const RELAY_RETRY_MS = 60_000

/** Where a new pane goes: <main>'s rect, or the window's while <main> has no size (the pane starts hidden). */
const paneAt = (): NonNullable<ReturnType<typeof relayPaneRect>> => {
  const viewport = { width: window.innerWidth, height: window.innerHeight }
  return relayPaneRect() ?? [{ x: 0, y: 0, ...viewport }, viewport]
}

export function useCrewWarmKeeper(prewarm: () => boolean) {
  const dispatch = useAppDispatch()
  const embedded = isEmbeddedPane()
  const activeId = useAppSelector(s => s.instances?.activeId ?? null)
  const relayActive = useAppSelector(s => s.instances?.relayActive ?? null)
  const warm = useAppSelector(s => s.instances?.warm ?? EMPTY_WARM)
  const relayLive = useAppSelector(s => s.instances?.relayLive ?? NO_IDS)
  const { crews } = useRelayViewerCrews(!embedded)
  const set = useCrewWarmSet()
  const setRef = useRef(set)
  setRef.current = set

  // Relay panes this keeper is closing. Every other pane that goes was closed by the person or by
  // the shell: that one is not prewarmed again until they open it. A click records its own open
  // (openRelayPane).
  const closing = useRef(new Set<string>())
  const closedByPerson = useRef(new Set<string>())
  // Prewarmed and not shown yet: the first show asks the shell again, which reloads a pane left
  // on the relay's error page (a 403 while the Crew came up) instead of showing it.
  const unseen = useRef(new Set<string>())
  const lastLive = useRef<string[]>(relayLive)

  useEffect(() => { if (!embedded && activeId) noteCrewOpened(sshCrewKey(activeId)) }, [embedded, activeId])

  useEffect(() => {
    if (embedded) return
    const before = lastLive.current
    lastLive.current = relayLive
    for (const id of relayLive) if (!before.includes(id)) closedByPerson.current.delete(id)
    for (const id of before) {
      if (relayLive.includes(id)) continue
      unseen.current.delete(id)
      if (!closing.current.delete(id)) closedByPerson.current.add(id)
    }
  }, [embedded, relayLive])

  useEffect(() => {
    if (embedded || !relayActive) return
    closedByPerson.current.delete(relayActive)
    noteCrewOpened(relayCrewKey(relayActive))
    if (!unseen.current.delete(relayActive)) return
    const bridge = relayPaneBridge()
    if (bridge) void bridge.openPane(relayActive, ...paneAt()).catch(() => {})
  }, [embedded, relayActive])

  // Fallen out of the set: the pane goes, the tunnel stays (an SSH Crew re-warms on its next
  // click), and a relay pane closes, its tab with it. The Crew in front is never dropped. A set
  // ranked before an open this render recorded waits for the next one, so it never drops that Crew.
  useEffect(() => {
    if (!set || set.order !== recentCrews()) return
    for (const id of Object.keys(warm)) {
      if (id !== activeId && !set.ssh.includes(id)) dispatch(removeWarm(id))
    }
    const bridge = relayPaneBridge()
    if (!bridge) return
    for (const id of relayLive) {
      if (id === relayActive || set.relay.includes(id) || closing.current.has(id)) continue
      closing.current.add(id)
      void bridge.closePane(id)
    }
  }, [set, warm, activeId, relayLive, relayActive, dispatch])

  // Prewarm: a hidden pane for each relay Crew in the set that is online and has none, one at
  // a time. The shell opens it as a click would, so it mints exactly the token a click would, and
  // answers ALREADY_OPEN without raising or re-minting for a Crew open anywhere. Each one writes a
  // `relay_viewer.token` audit row with no click. An older shell without the call prewarms nothing.
  const running = useRef(false)
  const lastTry = useRef<Record<string, number>>({})
  // Bumped RELAY_RETRY_MS after a failed prewarm, so it is tried again even when no list changed.
  const [retry, setRetry] = useState(0)
  const retryTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(retryTimer.current), [])
  const liveRef = useRef(relayLive)
  liveRef.current = relayLive
  useEffect(() => {
    const bridge = relayPaneBridge()
    if (!set || !bridge?.prewarmPane || running.current || !prewarm()) return
    const prewarmPane = bridge.prewarmPane.bind(bridge)
    const due = (id: string) =>
      !!setRef.current?.relay.includes(id) && !liveRef.current.includes(id) && !closedByPerson.current.has(id)
    const now = Date.now()
    const todo = crews.filter(c =>
      isRelayCrewOnline(c) && due(c.endpointId) && now - (lastTry.current[c.endpointId] ?? 0) >= RELAY_RETRY_MS)
    if (!todo.length) return
    running.current = true
    let failed = false
    void (async () => {
      try {
        for (const c of todo) {
          // The set may have moved while the last one opened (a Sign out, a new open).
          if (!due(c.endpointId) || !prewarm()) continue
          lastTry.current[c.endpointId] = Date.now()
          let ok = false
          try {
            ok = (await prewarmPane(c.endpointId, ...paneAt())).ok
          } catch { /* tried again after RELAY_RETRY_MS */ }
          if (!ok) { failed = true; continue }
          unseen.current.add(c.endpointId)
          // gen -1 never matches pickSeq: the tab is added hidden and nothing on screen changes.
          dispatch(relayPaneOpened({ id: c.endpointId, name: c.name, gen: -1 }))
        }
      } finally {
        running.current = false
        if (failed) {
          window.clearTimeout(retryTimer.current)
          retryTimer.current = window.setTimeout(() => setRetry(n => n + 1), RELAY_RETRY_MS)
        }
      }
    })()
  }, [set, crews, relayLive, dispatch, prewarm, retry])
}
