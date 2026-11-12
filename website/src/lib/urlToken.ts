/**
 * The dashboard's `?token=` sign-in credential, taken off the address bar
 * before the first render.
 *
 * The gateway sets the session cookie on the response to the page load that
 * carried the token, so nothing after boot needs it in the URL. Any screen
 * drawn before the chat page would otherwise keep it in the address bar and in
 * history, and the first-run setup gate holds the app back for as long as
 * setup takes. The chat page still reads the value once, for the prompt a
 * signed chat link carries.
 */

let urlToken: string | null = null

/** Read `?token=` and strip only that param from the address. Call once, before render. */
export function captureUrlToken(win: Pick<Window, 'location' | 'history'> = window): void {
  const url = new URL(win.location.href)
  urlToken = url.searchParams.get('token') || null
  if (!url.searchParams.has('token')) return
  url.searchParams.delete('token')
  win.history.replaceState(win.history.state, '', url.pathname + url.search + url.hash)
}

/** True while a captured token has not been taken yet. */
export function hasUrlToken(): boolean {
  return urlToken !== null
}

/** The captured token, once: every later call answers null. */
export function takeUrlToken(): string | null {
  const token = urlToken
  urlToken = null
  return token
}
