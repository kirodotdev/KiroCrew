import { useContext, useEffect, useMemo, useRef, type ContextType } from 'react'
import {
  UNSAFE_NavigationContext, parsePath, useLocation, useNavigate, useNavigationType, useParams,
  type Navigator, type To,
} from 'react-router-dom'
import { AlertTriangle, Loader2 } from 'lucide-react'
import { registerPopout, forwardToMain } from '../utils/appPopout'
import { appRouteOf, routerParamForm } from '../utils/appPopoutRoute'
import { withBase } from '../lib/basePath'
import { chatDeepLinkSlot, claimChatLaunch } from '../utils/navIntent'
import { consumeChatHandoff, installHardExit } from '../utils/errorReport'
import { hasBuiltinComponent } from '../apps/builtinRegistry'
import { appNavTarget } from '../appNav'
import { useInstalledApps } from '../hooks/panelTabRegistry'
import { useAppStore, type AppStore } from '../store'
import { setPendingInput } from '../store/chatSlice'
import AppPopoutTitleBar from '../components/AppPopoutTitleBar'
import ErrorNotice from '../components/ErrorNotice'
import { Btn } from '../components/ui'
import BuiltinAppRoute from '../apps/BuiltinAppRoute'
import AppPage from './AppPage'
import { i18nT } from '../i18n/t'

/**
 * The session an app in this window activated and no exit has carried yet.
 *
 * Apps can activate a session by switching to an existing slot or by creating,
 * forking, or directly selecting one. The store is this window's, so the slot
 * rides the hand-off instead, and the hand-off spends it so a later exit that
 * activated nothing carries nothing. A new `slotSwitchRequestId` records every
 * explicit switch, including a same-session re-open; an `activeSlot` change
 * records activation paths that do not dispatch `switchSlot`. Window-scoped,
 * like the store it shadows: the window renders one frame, pinned to one app,
 * and both hand-off paths below read it.
 */
let uncarriedSlotSwitch: string | null = null

/** Test-only: forget a recorded switch between cases. */
export function __resetForTests(): void { uncarriedSlotSwitch = null }

/**
 * Forwards one exit — an app-space `pathname` plus `search` — to the main
 * dashboard as a single intent: path and query, the chat slot it names, and
 * the four things an app's chat exit stages in THIS window before it
 * navigates, each of which a chat page here would have read and the main
 * window's cannot: the `pendingInput` a page puts in the store (Dev Fleet,
 * File Explorer), the session an app activated and then left for
 * (`uncarriedSlotSwitch`), the error prompts staged in this window's
 * sessionStorage, and the App SDK's `openChat` intent on
 * `window.__mc_chat_launch`. The main window re-stages each and performs the
 * same navigation, so the exit lands the way it would have locally. Each is
 * consumed here: it was staged for a page this window is not going to mount,
 * and left in place it would seed the next hand-off twice. With no main window
 * to claim the intent, the controller opens the destination in a new tab.
 */
function handOffToMain(store: AppStore, pathname: string, search: string): void {
  const pending = store.getState().chat.pendingInput
  // Spent by this hand-off whatever the destination: in main, too, a switch
  // followed by some other page is not what the chat page shows next.
  const switched = uncarriedSlotSwitch
  uncarriedSlotSwitch = null
  const errorHandoffs: string[] = []
  let errorHandoff: string | null
  while ((errorHandoff = consumeChatHandoff()) !== null) errorHandoffs.push(errorHandoff)
  const onChat = pathname === '/chat' || pathname.startsWith('/chat/')
  forwardToMain({
    path: pathname,
    search: search || undefined,
    slotKey: chatDeepLinkSlot(`${pathname}${search}`) || (onChat ? switched : null) || undefined,
    pendingInput: pending || undefined,
    ...(errorHandoffs.length ? { errorHandoffs } : {}),
    chatLaunch: claimChatLaunch(),
  })
  if (pending) store.dispatch(setPendingInput(null))
}

/**
 * `pathname` with the router `basename` taken off, matching the router's own
 * rule (case-insensitive, on a segment boundary). A navigator receives paths
 * with the basename already prepended (`/popout/app/chat`); the app's identity
 * and the main dashboard's routes are in app space (`/chat`). A path outside
 * the basename is returned unchanged.
 */
function appSpacePath(pathname: string, basename: string): string {
  if (basename === '/') return pathname
  const base = basename.endsWith('/') ? basename.slice(0, -1) : basename
  if (!pathname.toLowerCase().startsWith(base.toLowerCase())) return pathname
  const rest = pathname.slice(base.length)
  if (rest && !rest.startsWith('/')) return pathname
  return rest || '/'
}

/**
 * The router's navigation context with its navigator scoped to the pinned app.
 *
 * Every `useNavigate()`, `<Link>` and `<Navigate>` below it — an installed
 * app's SDK `navigate` included, which is the host's `useNavigate` — resolves
 * its target and then calls `navigator.push` / `replace` from this context. A
 * target on the pinned app goes to the real navigator unchanged; any other
 * target is an exit, handed to the main dashboard and never committed here. So
 * an exit never becomes this window's location, and nothing under the frame
 * unmounts for it: the app keeps its state, scroll position, open dialogs and
 * half-typed fields, which stepping back after a committed exit could not keep
 * (the destination had already replaced the app subtree). `go` (history
 * `-1`) passes straight through.
 *
 * Href building applies the same judgement without the hand-off. A modifier or
 * middle click, "open in new tab" and copy-link follow a `<Link>`'s rendered
 * `href` and never call `push`, so an exit's href must already be the main
 * dashboard's URL for that page (the build base plus the app-space path), not
 * one under this window's `/popout/app` basename, which would open a second
 * pop-out pinned to a page that is not an app. An in-app target keeps the
 * router's own href. `encodeLocation` passes through: the router calls it only
 * to percent-encode a pathname for route matching and active-link checks,
 * where the value must stay in this router's space.
 */
function usePinnedNavigation(route: string, store: AppStore): ContextType<typeof UNSAFE_NavigationContext> {
  const navigation = useContext(UNSAFE_NavigationContext)
  return useMemo(() => {
    const real = navigation.navigator
    /** `to` in app space and whether it leaves the pinned app; `null` for no pathname (the current one, the app's). */
    const judge = (to: To) => {
      const { pathname, search, hash } = typeof to === 'string' ? parsePath(to) : to
      if (pathname === undefined) return null
      const appPath = appSpacePath(pathname, navigation.basename)
      return { appPath, search: search ?? '', hash: hash ?? '', exit: appRouteOf(appPath, hasBuiltinComponent) !== route }
    }
    const exits = (to: To): boolean => {
      const target = judge(to)
      if (!target?.exit) return false
      handOffToMain(store, target.appPath, target.search)
      return true
    }
    const navigator: Navigator = {
      createHref: to => {
        const target = judge(to)
        if (!target?.exit) return real.createHref(to)
        return real.createHref({ pathname: withBase(target.appPath), search: target.search, hash: target.hash })
      },
      encodeLocation: real.encodeLocation ? to => real.encodeLocation!(to) : undefined,
      go: delta => real.go(delta),
      push: (to, state, opts) => { if (!exits(to)) real.push(to, state, opts) },
      replace: (to, state, opts) => { if (!exits(to)) real.replace(to, state, opts) },
    }
    return { ...navigation, navigator }
  }, [navigation, route, store])
}

/**
 * The window shell for a popped-out app (`/popout/app/<route>/…`).
 *
 * Renders the app exactly as the main dashboard does — `BuiltinAppRoute` for a
 * builtin, `AppPage` (the ESM `AppHost`) for an installed app — inside the same
 * `<main>` scroll container the shell gives every page, so the app's own layout
 * is unchanged. A separate JS context with its own store and socket, so it
 * stays live on its own; the gateway is the source of truth. On top it
 * (1) registers as the live pop-out for `route` so the main dashboard can
 * track / focus / bring it back, (2) mirrors the app's name into the OS
 * window/taskbar title, and (3) carries a title bar above `<main>` with the
 * app's name and the Return control.
 *
 * The window's router is mounted at `/popout/app` (see `appPopoutRoute.ts`), so
 * the app's absolute paths resolve here as they do in main. That also means an
 * app's EXIT — `navigate('/chat?sid=…')`, a link to Settings — would mount that
 * page in this window. It does not: `route` is this window's pinned app, and
 * the app subtree navigates through a navigator scoped to it
 * (`usePinnedNavigation`), which hands every other destination to the main
 * dashboard before the router commits it — the same forwarding the artifact
 * pop-out uses — and leaves this window, and the app's state, where they are.
 */
export default function AppPopoutFrame({ route }: {
  /** The app this window is pinned to, fixed at document load. */
  route: string
}) {
  const { builtinApp, name } = useParams<{ builtinApp?: string; name?: string }>()
  const store = useAppStore()
  const pinnedNavigation = usePinnedNavigation(route, store)
  // The page the CURRENT location resolves to, in the same terms as `route`.
  // Under the two arms App.tsx registers for this frame (`/apps/:name`,
  // `/:builtinApp/*`), one of the two params is always set.
  const here = name !== undefined ? `/apps/${name}` : `/${builtinApp ?? ''}`

  // `isLoadingError`, not `isError`: a background refetch that fails (a window
  // focus during a gateway restart) keeps the cached list, and the app on
  // screen keeps its judgement and its state. Only a list that never loaded
  // has nothing to judge from.
  const { apps, isPending: appsPending, isLoadingError: appsError, error: appsLoadError, refetch: refetchApps } = useInstalledApps()
  // `/apps/<name>` is the AppHost arm. `AppPage` redirects one kind of app off
  // it: a native builtin (one with no UI bundle), whose surface is at its own
  // page route. Here that redirect would be an exit: handed to main, with
  // nothing left on screen in this window. So for this arm the list is consulted
  // first, and such a route (or a name the list does not know) is refused like a
  // route that names no app. Judged from the three fields `AppPage` reads, with
  // no enabled check: it redirects a disabled builtin too. Until the list has
  // answered nothing mounts, and a list that failed gets a Retry notice
  // instead: `AppPage` cannot be the judge here, because its answer to a
  // native builtin is the very redirect this guard exists to stop.
  const onAppsArm = route.startsWith('/apps/')
  const listed = onAppsArm ? apps.find(app => `/apps/${app.name}` === route) : undefined
  const nativeBuiltin = listed?.origin === 'builtin' && !listed.manifest?.ui?.entry
    && !!listed.manifest?.ui?.pages?.[0]?.route
  const misrouted = onAppsArm && !appsPending && !appsError && (!listed || nativeBuiltin)
  // A deep link can name anything; only a route the main dashboard would render
  // as an app is one. Checked against the registry here rather than left to
  // `BuiltinAppRoute`, whose answer for an unknown path is a redirect to /chat —
  // an exit this window would forward to main, leaving an empty window here.
  const valid = appRouteOf(route, hasBuiltinComponent) === route && !misrouted
  const label = useMemo(() => {
    for (const app of apps) {
      const target = appNavTarget(app)
      if (target?.route === route) return target.label
    }
    return ''
  }, [apps, route])

  // Announce presence for `route` and wire the heartbeat/control responder.
  useEffect(() => registerPopout(route), [route])

  // Remember the session the app activates for the exit that follows it (see
  // `uncarriedSlotSwitch`). An explicit switch is identified by its request id,
  // including a same-key re-open. An active-slot change covers activation paths
  // such as createSlot.fulfilled, a fork, or setActiveSlot.
  useEffect(() => {
    const chat = store.getState().chat
    let seenRequest = chat.slotSwitchRequestId
    let seenActiveSlot = chat.activeSlot
    return store.subscribe(() => {
      const { activeSlot, slotSwitchRequestId, slotSwitchTarget } = store.getState().chat
      if (slotSwitchRequestId && slotSwitchRequestId !== seenRequest) {
        uncarriedSlotSwitch = slotSwitchTarget
      }
      if (activeSlot && activeSlot !== seenActiveSlot) uncarriedSlotSwitch = activeSlot
      seenRequest = slotSwitchRequestId
      seenActiveSlot = activeSlot
    })
  }, [store])

  // Reflect the app in the window/taskbar title so multiple pop-outs are
  // distinguishable at the OS level.
  const shownLabel = label || i18nT('pages.appPopoutFrame.app')
  useEffect(() => {
    document.title = i18nT('pages.appPopoutFrame.window_title', { label: shownLabel })
  }, [shownLabel])

  // Only a navigation that bypassed the pinned navigator lands here (see
  // `AppPopoutHandoff`): everything under the frame's own subtree is caught
  // before it commits. `here` comes from `useParams()`, which hands the path
  // back decoded, while `route` is the raw document path; compared as they
  // are, an encoded route (`/apps/a%20b` against `/apps/a b`) never matches,
  // and the hand-off's step back to `route` would land here again forever.
  if (here !== routerParamForm(route)) return <AppPopoutHandoff route={route} />

  return (
    <div className="h-screen w-screen overflow-hidden bg-bg flex flex-col relative">
      {/* Outside the pinned navigation context: the bar navigates nowhere, and
          Return goes through the pop-out channel, not the router. */}
      <AppPopoutTitleBar label={shownLabel} />
      {/* The same box the shell's <main> gives an app page (scrolling, not
          fixed-height: no app route is in the shell's fixed-height set), so the
          page lays out as it does in the main window. */}
      <main className="flex flex-col min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto">
        <UNSAFE_NavigationContext.Provider value={pinnedNavigation}>
          {onAppsArm && appsPending && !appsError ? (
            // No `AppPage` until the list has answered: mounting it first would
            // let its redirect race the judgement above. Meanwhile, the same
            // loading line `AppPage` shows, so the window is not blank.
            <div
              data-testid="app-popout-list-loading"
              role="status"
              className="flex-1 flex items-center justify-center text-muted text-sm"
            >
              <Loader2 size={16} className="animate-spin mr-2" aria-hidden="true" />
              {i18nT('pages.appPage.loading_app')}
            </div>
          ) : onAppsArm && appsError ? (
            // Retry here and Return in the title bar, like `AppPage`'s own
            // failed-load shell, so the window is not a dead end.
            <div className="flex-1 p-4 flex flex-col gap-3">
              <ErrorNotice
                title={i18nT('pages.appPopoutFrame.could_not_load_the_app_list')}
                message={appsLoadError instanceof Error ? appsLoadError.message : String(appsLoadError)}
                askAgent
              />
              <div>
                <Btn onClick={() => { void refetchApps() }}>{i18nT('pages.appPopoutFrame.retry')}</Btn>
              </div>
            </div>
          ) : !valid ? (
            <div className="flex flex-col items-center justify-center flex-1 gap-3 text-center p-8">
              <div className="text-4xl"><AlertTriangle className="lucide-inline" /></div>
              <div className="text-lg font-bold text-text-strong">
                {i18nT('pages.appPopoutFrame.no_app_at_route', { route })}
              </div>
              <div className="text-sm text-muted max-w-md">
                {i18nT('pages.appPopoutFrame.no_app_at_route_lead')}
              </div>
            </div>
          ) : route.startsWith('/apps/') ? (
            <AppPage />
          ) : (
            <BuiltinAppRoute />
          )}
        </UNSAFE_NavigationContext.Provider>
      </main>
    </div>
  )
}

/**
 * Hands an exit that the router already COMMITTED to the main dashboard, then
 * steps this window back to the app.
 *
 * Reached only by a navigation from outside the frame's subtree, which the
 * pinned navigator never sees: the shell's soft navigator (`appShell.tsx`,
 * `installSoftNavigate`), through which a hook-free "Ask the agent" control
 * sends this window to /chat. The app subtree has already been replaced by
 * the time this renders, so it remounts after the step back; exits from the
 * app itself never come this way. Nor does a hard hand-off (`sendErrorToChat`
 * with `hard`, the crashed-app boundary card): it reaches `handOffToMain`
 * directly through `installHardExit`, with no navigation in this window.
 *
 * Stepping back is a history `-1` after a PUSH (so the step restores the app's
 * sub-path as it was), and a `replace` to the app's route otherwise: after a
 * replace there is no entry behind this one that is the app's, and after a POP
 * the entry behind is unknowable, so the app's own route is the one safe
 * landing.
 *
 * Keyed on `location.key`, not mount: this element stays mounted while one
 * exit follows another, and each one is its own hand-off.
 */
function AppPopoutHandoff({ route }: { route: string }) {
  const location = useLocation()
  const navigate = useNavigate()
  const navigationType = useNavigationType()
  const store = useAppStore()
  const handled = useRef<string | null>(null)
  useEffect(() => {
    if (handled.current === location.key) return
    handled.current = location.key
    handOffToMain(store, location.pathname, location.search)
    if (navigationType === 'PUSH') navigate(-1)
    else navigate(route, { replace: true })
  }, [location.key, location.pathname, location.search, navigationType, navigate, store, route])
  return null
}

/**
 * Installs this window's hard exit: a hard "Ask the agent" (the boundary card
 * of a crashed app, `AskAgentButton … hard`) hands its prompt to the main
 * window the way the frame's other exits do, instead of loading /chat here
 * and turning the pop-out into a second dashboard. Rendered by App.tsx beside
 * the frame's routes, above the route boundary, so a crash of the frame
 * itself does not take the sink with it. `handOffToMain` drains the prompt
 * `sendErrorToChat` staged in this window's sessionStorage.
 */
export function AppPopoutHardExit() {
  const store = useAppStore()
  useEffect(() => {
    installHardExit(() => handOffToMain(store, '/chat', ''))
    return () => installHardExit(null)
  }, [store])
  return null
}
