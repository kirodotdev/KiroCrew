/**
 * instancesSlice — shared client state for the multi-instance header switcher.
 *
 * `warm` holds the in-memory loopback port + minted token for each connected
 * instance whose iframe is kept mounted (hide-not-unmount). It is NEVER
 * persisted and never logged — it lives only for the dashboard session, exactly
 * like the previous page-local state. The header tab strip writes it (on
 * connect) and the viewport reads it (to build iframe src + render).
 *
 * `activeId` is the instance currently filling the page body, or `null` for the
 * native dashboard (the "Local" tab). `mru` is recency order (front = most
 * recent) for K-cap eviction. `unread` is the validated postMessage relay count.
 *
 * `host` is the ONLY field written inside an embedded remote pane: the parent
 * dashboard relays the switcher model (tabs + which one is active + this pane's
 * own tunnel status + macOS traffic-light inset) down via postMessage so the
 * embedded header can render the instance switcher inline — exactly like the
 * local tab — instead of the parent stacking a second standalone strip on top
 * of the pane (option B). It is null in the top-level (non-embedded) dashboard.
 */
import { createSlice, type PayloadAction } from '@reduxjs/toolkit'

export interface WarmConn {
  port: number
  token: string
}

/** One switcher tab as relayed to an embedded pane (parent → frame). */
export interface HostTab {
  id: string
  name: string
  sshHost: string
  /** Live tunnel state driving the per-tab dot: connected|connecting|error|disconnected. */
  state?: string
  unread: number
}

/** The embedded pane's OWN tunnel status, used by its readout capsule (item 1). */
export interface HostSelfTunnel {
  state?: string
  /** Seconds of token life remaining (parent-owned). */
  ttlRemaining?: number
  /** Total token TTL in seconds. */
  ttlTotal?: number
}

/** Full model the parent relays to each embedded pane. */
export interface HostModel {
  tabs: HostTab[]
  activeId: string | null
  self: HostSelfTunnel | null
  /** True when the parent is a macOS Electron window not in fullscreen, so the
   *  embedded header must inset its content clear of the native traffic lights. */
  macInset: boolean
}

interface InstancesState {
  warm: Record<string, WarmConn>
  activeId: string | null
  mru: string[]
  unread: Record<string, number>
  host: HostModel | null
}

const initialState: InstancesState = {
  warm: {},
  activeId: null,
  mru: [],
  unread: {},
  host: null,
}

const instancesSlice = createSlice({
  name: 'instances',
  initialState,
  reducers: {
    setWarm(state, action: PayloadAction<{ id: string; conn: WarmConn }>) {
      state.warm[action.payload.id] = action.payload.conn
      state.mru = [action.payload.id, ...state.mru.filter(x => x !== action.payload.id)]
    },
    setActiveId(state, action: PayloadAction<string | null>) {
      state.activeId = action.payload
      if (action.payload) {
        state.mru = [action.payload, ...state.mru.filter(x => x !== action.payload)]
        // Selecting an instance clears its unread badge.
        if (state.unread[action.payload]) state.unread[action.payload] = 0
      }
    },
    /** Pure client-state teardown for one connection (no API call). */
    removeWarm(state, action: PayloadAction<string>) {
      const id = action.payload
      delete state.warm[id]
      delete state.unread[id]
      state.mru = state.mru.filter(x => x !== id)
      if (state.activeId === id) state.activeId = null
    },
    setUnread(state, action: PayloadAction<{ id: string; count: number }>) {
      state.unread[action.payload.id] = action.payload.count
    },
    /** Embedded panes only: store the switcher model relayed by the parent. */
    setHostModel(state, action: PayloadAction<HostModel | null>) {
      state.host = action.payload
    },
    clearInstances() {
      return initialState
    },
  },
})

export const { setWarm, setActiveId, removeWarm, setUnread, setHostModel, clearInstances } =
  instancesSlice.actions
export default instancesSlice.reducer
