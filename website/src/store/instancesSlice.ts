/**
 * instancesSlice — shared client state for the multi-instance header switcher.
 *
 * `warm` holds the in-memory loopback port + minted token for each connected
 * instance whose iframe is kept mounted (hide-not-unmount). It is NEVER
 * persisted and never logged — it lives only for the dashboard session. The
 * header tab strip writes it (on connect) and the viewport reads it (to build
 * iframe src + render).
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

// The shapes live in a neutral module, so the store does not depend on a page.
import type { InstanceDraft, InstanceFormValues } from '../types/instanceForm'

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
  /** The parent window's focus mode, relayed so the pane hides its own chrome to
   *  match instead of landing fully-framed inside a focused window. `null` means
   *  the host SENT NO OPINION — an older host whose model predates the field —
   *  and the pane must keep its own state: coercing absence to `false` would
   *  snap a user-toggled pane back off on every host re-broadcast, since an old
   *  host also ignores the pane's echoed `mc-set-focus-mode`. The pane's OWN
   *  `mc-focus-mode` setting is left untouched either way — this is the host's
   *  view preference, and the pane has its own localStorage (cross-origin iframe). */
  focusMode: boolean | null
  /** True when the parent shell is Electron. Gates the embedded ⌘/Ctrl+digit
   *  instance-switch chord: in a plain browser those chords are reserved for
   *  browser tab switching, so the pane must not bind (or advertise) them. */
  electron: boolean
  /** The crews the parent has pinned into header chips, by id (`__local__` for
   *  the local dashboard). Relayed so the embedded bar shows the same chips as
   *  the local bar instead of reading its own cross-origin-iframe localStorage,
   *  which the parent's toggle can never reach. An embedded toggle posts
   *  `mc-set-crew-pin` back up so the set stays one shared value across every
   *  pane. A plain array because postMessage cannot carry a Set. */
  pinnedCrews: string[]
  /** The parent's "keep tab order fixed" preference, relayed so the embedded bar
   *  applies the same ordering as the local bar instead of always reshuffling on
   *  switch (its own cross-origin-iframe localStorage the parent can never
   *  reach). An embedded toggle posts `mc-set-stable-order` back up so the value
   *  stays one shared preference across every pane.
   *
   *  Tri-state on purpose, exactly like `focusMode` above: `null` means the host
   *  SENT NO OPINION — an older parent whose model predates this field, and which
   *  therefore also has no `mc-set-stable-order` handler. Coercing that absence
   *  to `false` would leave the pane offering a toggle the host can never honor,
   *  so `null` orders by the pre-relay default AND hides the control instead. */
  stableOrder: boolean | null
}

/**
 * Unsaved Settings crew-form state, kept ABOVE the route that renders it.
 *
 * `RemoteCrewPanel` lives under `/settings/*`, so the error → agent hand-off's
 * navigation unmounts it along with anything half-typed. Component state cannot
 * survive that, and serialising a copy to storage answers a harder question than
 * the one being asked: a stored draft has to be re-measured against a server
 * record on the way back, and its slot key outlives every reload, so a crew
 * removed and re-added under the same name (ids are name slugs) inherits a
 * stranger's draft. State that simply never leaves memory has neither problem —
 * the baseline travels as the same object it was captured from, and the whole
 * thing dies with the tab.
 *
 * Deliberately NOT persisted: a full page reload is not the loss being fixed —
 * the hand-off is an in-app navigation — and the browser does not restore a
 * controlled React form across a reload either.
 */
interface CrewFormState {
  /** Add-form values, or `null` when the form holds nothing but its defaults. */
  add: InstanceFormValues | null
  /**
   * The crew being edited, its unsaved values and the record the edit OPENED on,
   * plus the rebase counter the form uses as its React key.
   *
   * `baseline` is carried rather than re-read: the form measures "changed"
   * against the record it opened on, so re-reading the live poll's newer copy
   * would count someone else's concurrent change as the user's own edit and
   * write back a field the user never touched.
   */
  edit: { id: string; draft: InstanceDraft; seq: number } | null
}

interface InstancesState {
  warm: Record<string, WarmConn>
  activeId: string | null
  mru: string[]
  unread: Record<string, number>
  /** Panes whose embedded SPA announced `mc-embedded-ready` for the CURRENT
   *  src (port+token). Cleared whenever the src changes (a reload is coming),
   *  so the viewport can tell a live pane from one still loading / dead. */
  ready: Record<string, boolean>
  host: HostModel | null
  crewForms: CrewFormState
}

const initialState: InstancesState = {
  warm: {},
  activeId: null,
  mru: [],
  unread: {},
  ready: {},
  host: null,
  crewForms: { add: null, edit: null },
}

const instancesSlice = createSlice({
  name: 'instances',
  initialState,
  reducers: {
    setWarm(state, action: PayloadAction<{ id: string; conn: WarmConn }>) {
      const { id, conn } = action.payload
      const prev = state.warm[id]
      // A new port/token changes the iframe src (srcFor), which reloads the
      // pane — its previous readiness no longer describes what's on screen.
      // Tests preload partial slices, so tolerate a missing `ready` map.
      if (!state.ready) state.ready = {}
      if (!prev || prev.port !== conn.port || prev.token !== conn.token) {
        delete state.ready[id]
      }
      state.warm[id] = conn
      state.mru = [id, ...state.mru.filter(x => x !== id)]
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
      if (state.ready) delete state.ready[id]
      state.mru = state.mru.filter(x => x !== id)
      if (state.activeId === id) state.activeId = null
    },
    /** The pane's embedded SPA mounted and announced `mc-embedded-ready`. */
    setPaneReady(state, action: PayloadAction<string>) {
      if (!state.ready) state.ready = {}
      state.ready[action.payload] = true
    },
    /**
     * The parent no longer believes this pane is loaded, without a src change.
     * `mc-embedded-ready` fires on mount, so a pane whose shell mounted but
     * whose session is unrecoverable counts as ready while showing nothing
     * usable; retracting that is what lets the load verdict surface.
     */
    clearPaneReady(state, action: PayloadAction<string>) {
      if (state.ready) delete state.ready[action.payload]
    },
    setUnread(state, action: PayloadAction<{ id: string; count: number }>) {
      state.unread[action.payload.id] = action.payload.count
    },
    /** Embedded panes only: store the switcher model relayed by the parent. */
    setHostModel(state, action: PayloadAction<HostModel | null>) {
      state.host = action.payload
    },
    /**
     * Hold (or drop, with `null`) the Add form's unsaved values.
     *
     * Written on every change rather than only when a button hands off: the
     * navigation unmounts the whole panel, so the crew rows above the form and the
     * viewport overlay destroy it just as thoroughly — as does a sidebar click.
     * Making it a property of the form covers every exit instead of the one wired.
     */
    setCrewAddForm(state, action: PayloadAction<InstanceFormValues | null>) {
      // Tests preload partial slices, so tolerate a missing container.
      if (!state.crewForms) state.crewForms = { add: null, edit: null }
      state.crewForms.add = action.payload
    },
    /** Hold (or drop, with `null`) the unsaved edit of one crew, with the record
     *  it was opened on. */
    setCrewEditForm(state, action: PayloadAction<CrewFormState['edit']>) {
      if (!state.crewForms) state.crewForms = { add: null, edit: null }
      state.crewForms.edit = action.payload
    },
    clearInstances() {
      return initialState
    },
  },
})

export const {
  setWarm,
  setActiveId,
  removeWarm,
  setPaneReady,
  clearPaneReady,
  setUnread,
  setHostModel,
  setCrewAddForm,
  setCrewEditForm,
  clearInstances,
} = instancesSlice.actions
export default instancesSlice.reducer
