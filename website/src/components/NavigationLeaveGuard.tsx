import React from 'react'
import { useLocation } from 'react-router-dom'

/** The mounted page's answer to "may I navigate away from you?". `true` allows
 *  the navigation, `false` keeps the user exactly where they are. */
export type NavigationLeaveGuard = () => boolean

type Channel = {
  register: (guard: NavigationLeaveGuard) => () => void
  ask: () => boolean
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
 * That makes coverage OPT-IN per navigation surface, which is the known cost of
 * this shape: an in-app `navigate()` caller that does not ask still discards a
 * draft, and forgetting to ask fails silently. The exits wired today are this
 * layout's rail and mobile back bar, the global sidebar's `NavItem`, and the
 * command palette's `usePaletteActions` delegate. Browser Back/Forward cannot be
 * reached from here at all. Retiring the per-caller model -- a data router so
 * `useBlocker` becomes available, or lifting the draft so no exit destroys it --
 * is tracked in #8010; prefer adding to that over adding a fourth asker here.
 */
export function NavigationLeaveGuardProvider({ children }: { children: React.ReactNode }) {
  // One slot, not a registry: exactly one page is on screen at a time, so two
  // simultaneous registrants cannot exist. Cleanup is identity-checked, which
  // is what stops an outgoing page's unmount from clearing the incoming page's
  // guard if the two ever interleave.
  const guard = React.useRef<NavigationLeaveGuard | null>(null)
  const channel = React.useMemo<Channel>(() => ({
    register: g => {
      guard.current = g
      return () => { if (guard.current === g) guard.current = null }
    },
    // A page with nothing at stake registers no guard and this is a bare
    // `true`. The guard may show a confirm, so callers must only ever ask from
    // an event handler — never during render.
    ask: () => guard.current?.() !== false,
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
