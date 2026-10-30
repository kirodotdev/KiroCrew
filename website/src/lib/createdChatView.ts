import type { AppDispatch, RootState } from '../store'
import { createSlot } from '../store/chatSlice'
import type { ChatSlot } from '../types'
import { isChatPath } from '../hooks/notificationBanner'
import { appPathname } from './basePath'

type CreateSlotArg = Parameters<typeof createSlot>[0]

/** A create that resolved, and whether the user is now looking at it. */
export interface CreatedChatView {
  slot: ChatSlot
  /** The create switched the chat to its new session, and the user is still
   *  on the page where they asked for it. Only then may the caller show the
   *  session or put anything in its composer. */
  shown: boolean
  /** The create switched the chat to its new session, whatever page the user
   *  is on. A surface that also tracks its own "where the user is", such as
   *  the embed tab strip's tab, checks that alongside `shown`. */
  opened: boolean
  /** The app route the user was on when the create started. */
  from: string
}

/** The chat pages whose `/<prefix>/<slug>` follows `activeSlot`, so a create
 *  rewrites the slug without the user changing page. Used only to group a
 *  route by page; whether a route is a chat surface at all is `isChatPath`.
 *  A popout or embed window mounts the same shell keyboard and palette as the
 *  dashboard, and its router redirects any other route back into its own
 *  frame, so it must never be sent to `/chat`. */
const SELF_ROUTING_CHAT_PAGES = ['/chat', '/popout/chat', '/embed/chat']

/** An embed tab shows either its session at `/embed/chat/<slug>` or, while it
 *  has none, the Sessions list. When a create opens a session from a Sessions
 *  tab, the tab strip fills that tab and routes it to the chat, so both routes
 *  are the one tab page the user stayed on. */
const SAME_PAGE_AS: Record<string, string> = { '/embed/sessions': '/embed/chat' }

/** The frames whose router sends any route outside the frame back into it. */
const FRAME_PREFIXES = ['/popout/', '/embed/']

function chatPageOf(pathname: string): string | null {
  return SELF_ROUTING_CHAT_PAGES.find(p => pathname === p || pathname.startsWith(`${p}/`)) ?? null
}

/** The page a route shows. Every `<chat page>/<slug>` is that one chat page:
 *  its slug follows the active session, so the create itself rewrites it. */
function pageOf(pathname: string): string {
  return chatPageOf(pathname) ?? SAME_PAGE_AS[pathname] ?? pathname
}

/**
 * Run a foreground `createSlot` and report whether its session took the view.
 *
 * A create can take seconds. If the user opened another session meanwhile,
 * `createSlot.fulfilled` lists the new session but leaves the view alone
 * (`forkMayOpen`), and `lastCreatedActivation` then names some other request
 * or none. If the user went to another page, the store may still switch the
 * chat, but the user is no longer where they asked. In either case `shown` is
 * false, so the caller neither navigates nor stages composer input that would
 * land in the session the user moved to.
 *
 * The route is read from `window.location` rather than the router: the caller
 * is often a palette or command bar that has unmounted by the time the create
 * settles, and the router commits a navigation inside a transition, so its
 * rendered location can lag the address bar.
 */
export async function createChatForView(
  dispatch: AppDispatch,
  getState: () => Pick<RootState, 'chat'>,
  arg: CreateSlotArg,
): Promise<CreatedChatView> {
  const from = appPathname()
  const pending = dispatch(createSlot(arg))
  const slot = await pending.unwrap()
  const { lastCreatedActivation, activeSlot } = getState().chat
  const opened = !!slot?.key && lastCreatedActivation?.requestId === pending.requestId && activeSlot === slot.key
  return { slot, opened, shown: opened && pageOf(appPathname()) === pageOf(from), from }
}

/**
 * Bring the user to a create that took the view. A chat page already shows
 * the new session and writes its `?sid=`, so a create started there navigates
 * nowhere: an extra push of `/chat` would add a history entry that Back lands
 * on instead of the session the user came from. A popout or embed frame never
 * navigates to `/chat` from any of its pages: its router would redirect that
 * out of the chat, and the frame shows the session on its own. Returns `shown`.
 *
 * The page the user stayed on may have started holding a draft while the
 * create ran, so `navigate` must ask the leave guard when it is called
 * (`useAskingNavigate`), not before the create. A `navigate` that returns
 * `false` was refused: the created chat is kept, the user stays put, and this
 * returns `false` so the caller stages nothing for a session not on screen.
 */
export function showCreatedChat(view: CreatedChatView, navigate: (route: string) => boolean | void): boolean {
  const framed = FRAME_PREFIXES.some(p => view.from.startsWith(p))
  if (!view.shown || framed || isChatPath(view.from)) return view.shown
  return navigate('/chat') !== false
}
