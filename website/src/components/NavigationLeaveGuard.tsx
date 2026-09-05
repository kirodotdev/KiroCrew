import React from 'react'
import { useLocation, useNavigate, useNavigationType } from 'react-router-dom'

/** The mounted page's answer to "may I navigate away from you?". `true` allows
 *  the navigation, `false` keeps the user exactly where they are. */
export type NavigationLeaveGuard = () => boolean

type Channel = {
  register: (guard: NavigationLeaveGuard) => () => void
  ask: () => boolean
  /** Publish whether the page on screen is holding work an exit would destroy.
   *  Separate from `ask` because it must answer WITHOUT prompting: the browser
   *  Back guard needs to know there is something at stake before the user
   *  presses anything, and asking to find out would pop a confirm over a
   *  keystroke. */
  publishStake: (atStake: boolean) => void
  subscribeStake: (listener: (atStake: boolean) => void) => () => void
}

/** Null outside a provider, so both hooks below degrade to no-ops for a surface
 *  rendered standalone (tests, embedded uses) rather than crashing. */
const NavigationLeaveGuardContext = React.createContext<Channel | null>(null)

/**
 * Let the page currently on screen veto an in-app navigation that would unmount
 * it and destroy work the user typed.
 *
 * This is the same contract as `useSidePanelLeaveGuard`, one level further out.
 * That guard covers the exits a `SidePanelLayout` owns — its own tab rail and
 * mobile back bar — and `beforeunload` covers a real document unload. Neither
 * sees a CLIENT-SIDE ROUTE CHANGE: the global sidebar swaps the whole page
 * without the document ever unloading, so `beforeunload` never fires, and the
 * click belongs to the app shell rather than to any layout inside it.
 *
 * react-router's `useBlocker` is the mechanism that would normally answer this,
 * but it requires a data router (it reads `useDataRouterContext`) and the
 * dashboard mounts a plain `<BrowserRouter>`. So the veto is published the way
 * the pane-level one already is: the surface at risk registers an answer, and
 * the shell asks before it navigates.
 *
 * The exits wired to that answer are this layout's rail and mobile back bar, the
 * global sidebar's `NavItem`, the command palette's `usePaletteActions`
 * delegate, every notification-panel jump (through `useGuardedLeave`),
 * `SettingsLink` — the one declarative Settings deep link, which asks once for
 * every prose link built on it (also through `useGuardedLeave`) — and the
 * browser's own Back/Forward button (through `NavigationBackGuard`, which needs
 * the page to publish a stake — see `usePublishNavigationStake`).
 *
 * Coverage is still OPT-IN per navigation surface, which is the known cost of
 * this shape: an in-app `navigate()` caller that does not ask still discards a
 * draft, and forgetting to ask fails silently. A new surface should reach for
 * `useGuardedLeave` rather than hand-rolling the ask. Retiring the per-caller
 * model outright — a data router so `useBlocker` becomes available, or lifting
 * the draft so no exit destroys it — is tracked in #8010.
 */
export function NavigationLeaveGuardProvider({ children }: { children: React.ReactNode }) {
  // One slot, not a registry: exactly one page is on screen at a time, so two
  // simultaneous registrants cannot exist. Cleanup is identity-checked, which
  // is what stops an outgoing page's unmount from clearing the incoming page's
  // guard if the two ever interleave.
  const guard = React.useRef<NavigationLeaveGuard | null>(null)
  // Kept in refs and pushed to listeners rather than held in state: the answer
  // flips on the keystroke that first dirties a draft, and state here would
  // re-render the whole app under this provider to tell one listener.
  const stake = React.useRef(false)
  const stakeListeners = React.useRef(new Set<(atStake: boolean) => void>())
  const channel = React.useMemo<Channel>(() => ({
    register: g => {
      guard.current = g
      return () => { if (guard.current === g) guard.current = null }
    },
    // A page with nothing at stake registers no guard and this is a bare
    // `true`. The guard may show a confirm, so callers must only ever ask from
    // an event handler — never during render.
    ask: () => guard.current?.() !== false,
    publishStake: atStake => {
      if (stake.current === atStake) return
      stake.current = atStake
      // Iterated over a copy: a listener may unsubscribe from inside its own
      // callback, and mutating the live set mid-iteration would skip the next
      // listener.
      for (const listener of [...stakeListeners.current]) listener(atStake)
    },
    subscribeStake: listener => {
      stakeListeners.current.add(listener)
      // Delivered immediately, so a listener does not have to mount before the
      // page it is listening for. Without it, a guard mounted after a dirty
      // page (a remount, a StrictMode re-run) would sit disarmed.
      listener(stake.current)
      return () => { stakeListeners.current.delete(listener) }
    },
  }), [])
  return (
    <NavigationLeaveGuardContext.Provider value={channel}>
      {children}
    </NavigationLeaveGuardContext.Provider>
  )
}

/** Publish this surface's veto to the app shell. */
export function useRegisterNavigationLeaveGuard(guard: NavigationLeaveGuard) {
  const channel = React.useContext(NavigationLeaveGuardContext)
  // Register a stable trampoline over a ref, not `guard` itself: the guard
  // closes over the draft, so a new closure arrives on every keystroke.
  // Registering it directly would either re-run the effect per keystroke or
  // (with an empty dep list) pin the FIRST render's closure and read an empty
  // draft forever — losing exactly the text this exists to protect.
  const latest = React.useRef(guard)
  latest.current = guard
  React.useEffect(() => {
    if (!channel) return
    return channel.register(() => latest.current())
  }, [channel])
}

/**
 * Publish whether this surface is holding work right now.
 *
 * The guard above answers "may I leave?" by ASKING the user, which is only ever
 * safe from an event handler. `NavigationBackGuard` needs the answer one step
 * earlier — before any gesture — so it can arm itself while there is something
 * to lose and stay completely out of the history stack while there is not. A
 * page that registers a guard but publishes no stake keeps its old behaviour:
 * every wired in-app exit asks, and Back does not.
 */
export function usePublishNavigationStake(atStake: boolean) {
  const channel = React.useContext(NavigationLeaveGuardContext)
  React.useEffect(() => { channel?.publishStake(atStake) }, [channel, atStake])
  // Unmount-only, and deliberately NOT folded into the effect above, whose
  // cleanup also runs on every flip of `atStake`: a page that is gone holds
  // nothing, and leaving its last `true` published would keep the Back guard
  // armed for a draft that no longer exists.
  React.useEffect(() => () => { channel?.publishStake(false) }, [channel])
}

const ALWAYS_MAY_LEAVE = () => true

/** Ask the page on screen before an action that navigates away from it. */
export function useMayLeaveForNavigation(): () => boolean {
  const channel = React.useContext(NavigationLeaveGuardContext)
  return channel?.ask ?? ALWAYS_MAY_LEAVE
}

/**
 * Is this target the address we are already at, in full?
 *
 * The companion to `useMayLeaveForNavigation`: a navigation that changes nothing
 * unmounts nothing, and asking about it pops a discard-confirm the user never
 * earned. The test is the WHOLE address — pathname AND query — because a pane is
 * routinely mounted on the query (`{tab === 'prompts' && ...}` behind
 * `?tab=prompts`), so navigating from `/capabilities?tab=prompts` to a bare
 * `/capabilities` is a real unmount even though the pathname never moved. An
 * earlier revision compared the pathname alone and silently discarded exactly
 * the draft this channel exists to protect.
 */
export function useIsCurrentUrl(): (target: string) => boolean {
  const location = useLocation()
  const here = location.pathname + location.search
  return React.useCallback((target: string) => target === here, [here])
}

/**
 * Ask once, then run a whole handler that leaves the page.
 *
 * The gate goes in FRONT of the handler, not around its `navigate` call. A
 * wrapper over `useNavigate` can only veto the navigation, and these handlers do
 * more than navigate: `dispatch(switchSlot(s)); navigate('/chat')` would still
 * switch the slot when the user answers "keep my draft" — the draft survives, but
 * the app moved anyway, and the next visit to Chat lands somewhere they never
 * agreed to go. `await dispatch(resumeFromHistory(...))` in that position has
 * already resumed a session server-side, and `navigate(...); onClose()` closes
 * the panel the user was reading. Asking first makes the answer mean what it
 * says: nothing in the handler runs unless the page agreed to be left.
 *
 * Use a plain `useNavigate` inside — the ask has already happened.
 *
 * `to` is optional and only for the skip `useIsCurrentUrl` exists for: pass it
 * when the handler's target is data-driven and could be the address already on
 * screen, so a note pointing at the current page does not pop a discard-confirm
 * over a click that unmounts nothing.
 */
export function useGuardedLeave(): (perform: () => void | Promise<void>, to?: string) => void {
  const mayLeave = useMayLeaveForNavigation()
  const isCurrentUrl = useIsCurrentUrl()
  return React.useCallback((perform: () => void | Promise<void>, to?: string) => {
    if (!(to !== undefined && isCurrentUrl(to)) && !mayLeave()) return
    // A returned promise is deliberately not awaited: the gate answers "may this
    // run", and the handler owns its own async failure path (each one already
    // catches and logs).
    void perform()
  }, [mayLeave, isCurrentUrl])
}

/**
 * The router state a history entry this guard minted carries.
 *
 * The marker is what makes such an entry recognisable when its location is read
 * back — including across a reload, where it is the only thing that survives to
 * identify one. Declared as a TYPE with an identifier key rather than a string
 * constant: the key is a router contract no user ever reads, and spelling it as a
 * quoted literal would make the i18n gate charge it as untranslated copy.
 */
type TrapEntryState = { __navigationLeaveTrap?: true }

const isTrapEntry = (state: unknown): boolean =>
  !!(state && typeof state === 'object' && (state as TrapEntryState).__navigationLeaveTrap === true)

/**
 * The router's own per-entry bookkeeping, read straight from the platform.
 *
 * Not from a render: `popstate` fires BEFORE React commits the new location, so
 * a rendered value still describes the entry the user just left. `idx` is
 * react-router's stack position and `usr` the state a navigation carried.
 */
const routerEntry = (): { idx: number | null; state: unknown } => {
  const raw = window.history.state as { idx?: unknown; usr?: unknown } | null
  return { idx: typeof raw?.idx === 'number' ? raw.idx : null, state: raw?.usr }
}

/** The whole address, the way this guard compares two entries. */
const addressOf = (l: { pathname: string; search: string; hash: string }): string =>
  l.pathname + l.search + l.hash

/** Did this document arrive by a plain navigation, rather than a reload or a
 *  Back/Forward into it? Only then is the entry it landed on known to be the TOP
 *  of the stack — a navigation truncates, a reload preserves whatever was above.
 *  Absent timing data answers "unknown", never "yes". */
const arrivedByFreshNavigation = (): boolean => {
  try {
    const entries = performance.getEntriesByType('navigation') as { type?: string }[]
    return entries[0]?.type === 'navigate'
  } catch { return false }
}

/**
 * Route the browser's own Back/Forward button through the same veto.
 *
 * Back is the one exit nothing else here can reach. `beforeunload` is silent
 * (the document never unloads), the gesture belongs to no component, and
 * `useBlocker` — the mechanism built for exactly this — needs a data router the
 * dashboard does not mount. What is left is the stack itself: while the page on
 * screen has work at stake, this keeps ONE duplicate entry for the address it is
 * already at, so the first Back lands on the page's real entry with the address
 * unchanged and the page still mounted, draft intact. That pop is a real user
 * gesture, so it is safe to ask there — and the answer decides whether the Back
 * the user pressed is carried out or undone.
 *
 * Two rules keep it out of the way, and both are about NOT taking anything from
 * the user:
 *
 *  - it arms only while a stake is published, so a page with nothing to lose
 *    (every page today except a dirty prompt editor) never gets a duplicate
 *    entry, and Back, Forward, the mobile drill-in stack and every
 *    `location.key` consumer behave exactly as before;
 *  - it pushes only when it can PROVE the push destroys nothing. A push truncates
 *    everything above the current entry, and this one fires on a KEYSTROKE, so
 *    pushing while the user has a Forward branch would make typing throw away
 *    history they can never get back. Proving that needs the count of entries
 *    BELOW the app, which the platform never reports directly — so it is
 *    calibrated from moments where "nothing is above" is true by construction: a
 *    push (which truncates) and a document that arrived by a fresh navigation
 *    (likewise). Until one of those is seen — after a reload, or after arriving
 *    by Back — the count is unknown and the guard stays out of the stack
 *    entirely, leaving Back exactly as unguarded as it was before. A gap, not a
 *    loss.
 *
 * A duplicate it minted is never a destination: landing on one (a Back that
 * passes over one left buried by an earlier confirmed exit) continues in the same
 * direction rather than spending the user's press on an entry they cannot see.
 * And it stays OWNED for as long as it exists, which is what lets the guard
 * replace its own stale duplicate when a draft is dirtied again — a duplicate it
 * had forgotten would be indistinguishable from a Forward branch the user built,
 * so the truncation test would refuse to arm for the rest of the document.
 *
 * Ownership is a claim about an entry, so it is only trusted while that entry still
 * carries the marker: a replace-write overwrites the state of whatever entry it
 * lands on, which can turn a duplicate into a real destination without the guard
 * moving at all. Re-checked on every location change, and a page still holding work
 * gets a fresh duplicate the moment its old one stops being its own — anything less
 * leaves a page believing it is guarded when it is not.
 *
 * Mount inside the router, once. This is a mechanism of last resort for the
 * gesture no caller owns — an in-app `navigate()` should be wired through
 * `useGuardedLeave` instead, which needs no history entries at all.
 */
export function NavigationBackGuard() {
  const channel = React.useContext(NavigationLeaveGuardContext)
  const navigate = useNavigate()
  const location = useLocation()
  const navigationType = useNavigationType()
  // The whole address, not the pathname: a trap pushed at the pathname alone
  // would drop the `?tab=` the pane is mounted on, and the "duplicate" entry
  // would itself be the unmount it exists to prevent (see `useIsCurrentUrl`).
  const here = addressOf(location)
  // Read through a ref: the listeners below are registered once, so closing over
  // a render's values would pin the first one forever.
  const latest = React.useRef({ here, state: location.state as unknown })
  latest.current = { here, state: location.state as unknown }
  const armed = React.useRef(false)
  // The STACK POSITION of the duplicate this guard minted, held for as long as
  // that entry exists. Position rather than address, because the duplicate shares
  // its address with the entry beneath it by construction: only the index can say
  // whether a pop landed ON the duplicate, landed exactly UNDER it (so the press
  // consumed it), or sailed past it — and `armed` alone says none of the three,
  // which is how a long-press Back menu or `history.go(-3)` would otherwise be
  // mistaken for a single Back onto a page that is already unmounted.
  //
  // Held rather than dropped once passed, because OWNERSHIP is what makes the
  // entry replaceable: a duplicate the guard has forgotten is indistinguishable
  // from a Forward branch the user built, so the truncation test would refuse to
  // arm ever again and leave a re-dirtied editor unguarded.
  const trapIdx = React.useRef<number | null>(null)
  // Set while a move this guard issued is in flight. A programmatic `go(-1)` /
  // `forward()` fires `popstate` exactly like a press, and reading it as one is
  // what would turn a single carry-through into a walk down the stack.
  const selfMove = React.useRef(false)
  // Set while a push this guard issued is in flight, so the recalibration effect
  // does not read its own new duplicate as a stranger's entry and disown it.
  const selfPush = React.useRef(false)
  // How many entries sit BELOW the app's own first entry. Unknowable in general
  // (the platform reports only a total), so it is only ever taken at a moment
  // where nothing is above by construction — see the contract above. `null`
  // means "not established", which disables arming rather than guessing.
  const entriesBelow = React.useRef<number | null>(null)
  // The stack position of the entry we were on before the current pop, which is
  // how a Back is told from a Forward.
  const lastIdx = React.useRef<number | null>(null)

  /** Take the below-count from a moment where nothing can be above us. */
  const calibrate = React.useCallback(() => {
    const { idx } = routerEntry()
    if (idx !== null) entriesBelow.current = window.history.length - 1 - idx
  }, [])

  /** Entries above this one, or null while the baseline is unknown. */
  const above = React.useCallback((): number | null => {
    const { idx } = routerEntry()
    const below = entriesBelow.current
    if (idx === null || below === null) return null
    return window.history.length - 1 - idx - below
  }, [])

  /** Is a push here free? `ownedAbove` discounts entries above that this guard
   *  minted itself and may replace — one, when re-trapping right after its own
   *  duplicate was popped. */
  const mayPush = React.useCallback((ownedAbove: number): boolean => {
    const count = above()
    return count !== null && count - ownedAbove <= 0
  }, [above])

  /** Move the stack ourselves, marked so the resulting `popstate` is not read as
   *  a user press. */
  const selfGo = React.useCallback((direction: -1 | 1) => {
    selfMove.current = true
    if (direction === -1) window.history.go(-1)
    else window.history.forward()
  }, [])

  const pushTrap = React.useCallback(() => {
    armed.current = true
    selfPush.current = true
    // Pushed through the ROUTER rather than `history.pushState`: react-router
    // keeps its own index inside `history.state`, and a raw push overwrites it,
    // leaving the router's stack bookkeeping wrong for every later navigation.
    //
    // Carries ONLY this marker — the entry underneath keeps its own state, and
    // this one must not impersonate it. A trap that copied a mobile drill-in's
    // SUBNAV_PUSH_STATE would make `SidePanelLayout.backToRoot` take its POP
    // branch on an entry this guard minted: the pop would consume the trap, land
    // on the identical address, and read as a back bar that did nothing (with a
    // second confirm on top of the one the back bar already asked). Without the
    // marker those readers take their replace branch, which is the one written
    // for an entry they did not mint — and it lands on the right screen.
    navigate(latest.current.here, { state: { __navigationLeaveTrap: true } satisfies TrapEntryState })
    // Read back rather than assumed: `navigate` writes through to the platform
    // synchronously, and the position it landed on is the duplicate's identity for
    // every decision below.
    trapIdx.current = routerEntry().idx
  }, [navigate])

  // Track the stack position, and recalibrate on every PUSH. A push truncates,
  // so the entry it lands on has nothing above it — which is exactly the moment
  // the below-count is measurable. Runs after commit, so the popstate handler
  // still sees the PREVIOUS position while it is deciding.
  React.useEffect(() => {
    const { idx, state } = routerEntry()
    if (navigationType === 'PUSH') {
      calibrate()
      // A push truncates everything above the entry it lands on, so a duplicate
      // that sat up there is gone — unless this push IS the duplicate.
      if (selfPush.current) selfPush.current = false
      else if (trapIdx.current !== null && idx !== null && trapIdx.current >= idx) trapIdx.current = null
    }
    // A remembered position is only ever trusted while the entry it names still
    // SAYS it is ours. A replace overwrites the state of the entry it lands on —
    // `SidePanelLayout.backToRoot` does exactly that on a drill-in entry, and the
    // tab sync and rail write replaces too — so a duplicate can quietly become a
    // real destination underneath the guard. Believing otherwise makes the next
    // Back carry the user PAST a page they asked for.
    if (trapIdx.current !== null && idx !== null && idx === trapIdx.current && !isTrapEntry(state)) {
      trapIdx.current = null
      // The stake is still live (that is what `armed` means) and the entry that
      // was defending it is gone, so mint a replacement now. Without this the page
      // would believe it is guarded while Back walks straight out of it — and the
      // stake only re-publishes when it CHANGES, so nothing else would notice.
      if (armed.current) {
        armed.current = false
        if (mayPush(0)) pushTrap()
      }
    }
    lastIdx.current = idx
  }, [location.key, navigationType, calibrate, mayPush, pushTrap])

  // A document that arrived by a plain navigation landed on the top of the stack
  // for the same reason. A reload, or an arrival by Back, tells us nothing —
  // there the count stays unknown until the first push.
  //
  // The same mount adopts a duplicate that outlived the document. `trapIdx` is a
  // ref, so a reload while standing on one loses its position while the ENTRY
  // survives in the stack — and an unrecognised duplicate is an invisible entry
  // that spends the user's next Back press. The marker it carries is what
  // identifies it across a document boundary; adopting it also lets a re-dirtied
  // editor re-arm in place, which is the only protection available after a reload
  // (the below-count is unknown there, so nothing new may be pushed).
  React.useEffect(() => {
    if (entriesBelow.current === null && arrivedByFreshNavigation()) calibrate()
    const { idx, state } = routerEntry()
    if (idx !== null && isTrapEntry(state)) trapIdx.current = idx
  }, [calibrate])

  React.useEffect(() => {
    if (!channel) return
    return channel.subscribeStake(atStake => {
      // Compared against `armed`, not against a previous value: the immediate
      // delivery on subscribe (and StrictMode's re-subscribe) would otherwise
      // act twice on one stake.
      if (atStake === armed.current) return
      if (!atStake) {
        // Nothing at stake any more — saved, or dirtied and undone in place. The
        // duplicate STAYS: popping it here would either fight a navigation the
        // user just confirmed, or leave a stale entry above us that the
        // truncation test would then read as the user's Forward branch, so the
        // next dirty keystroke could never arm again. It is remembered instead,
        // and skipped if the user ever pops onto it.
        armed.current = false
        return
      }
      const idx = routerEntry().idx
      // Dirty again while our own duplicate is still the entry we are standing
      // on (type a character, undo it, type another): re-arm the duplicate that is
      // already there rather than stacking a second one.
      if (trapIdx.current !== null && trapIdx.current === idx) {
        armed.current = true
        return
      }
      // A duplicate of ours sitting directly above is OURS to replace, so it does
      // not count against the truncation test — the alternative is refusing to
      // arm for the rest of the document because of an entry this guard minted
      // itself (clear the draft, Back, Forward, edit again).
      const ownedAbove = trapIdx.current !== null && idx !== null && trapIdx.current === idx + 1 ? 1 : 0
      if (mayPush(ownedAbove)) pushTrap()
    })
  }, [channel, pushTrap, mayPush])

  React.useEffect(() => {
    if (!channel) return
    const onPop = () => {
      const { idx } = routerEntry()
      const from = lastIdx.current
      const trap = trapIdx.current
      // A move this guard issued, arriving back as a `popstate`. Not a press, so
      // it decides nothing — reading it as one is what would turn a single
      // carry-through into a walk down the stack.
      if (selfMove.current) { selfMove.current = false; lastIdx.current = idx; return }
      // No duplicate of ours in play (or no index to reason with): this pop is
      // somebody else's business.
      if (trap === null || idx === null) { armed.current = false; return }

      if (idx === trap) {
        // Landed ON the duplicate. It renders the same address as the entry
        // beneath it, so stopping here spends the user's press on a move they
        // cannot see — carry on the way they were going. Ownership is KEPT: the
        // entry still exists, and this guard is the only thing allowed to replace
        // it.
        armed.current = false
        selfGo(from !== null && idx > from ? 1 : -1)
        return
      }

      if (idx !== trap - 1 || from !== trap) {
        // Either the pop went PAST the duplicate (a multi-entry move: the page
        // this guard was defending is already unmounted, so there is nothing left
        // to ask about), or it arrived from somewhere else entirely — a Forward
        // onto the page's own entry, say — which consumed nothing. Stand down
        // without touching the stack.
        armed.current = false
        return
      }

      // Came from the duplicate and landed exactly under it: this press consumed
      // it, and the page is still mounted with its text at the same address.
      if (!armed.current) {
        // Nothing at stake — saved, or dirtied and undone. The duplicate was
        // invisible, so finish the move the user actually asked for.
        selfGo(-1)
        return
      }
      // Disarmed FIRST, so the programmatic moves below are not mistaken for a
      // second Back press.
      armed.current = false
      if (channel.ask()) {
        // Allowed. The duplicate absorbed the pop the user made, so the real one
        // is still owed. (A no-op when the page's own entry is the oldest in the
        // session — there was nowhere to go back to in the first place.)
        selfGo(-1)
      } else if (mayPush(1)) {
        // Vetoed: the user stays, and a fresh duplicate makes sure the NEXT Back
        // is caught too. The one entry above is the duplicate this pop just left —
        // this guard's own, so replacing it truncates nothing of the user's.
        pushTrap()
      }
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [channel, pushTrap, mayPush, selfGo])

  return null
}
