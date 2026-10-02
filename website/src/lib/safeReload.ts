/**
 * Safe reload: open the dashboard WITHOUT reopening the chat it remembered.
 *
 * The desktop shell reloads the dashboard after its renderer crashes or hangs
 * (see `website/electron/renderer-recovery.js`). If the remembered chat is the
 * thing that froze the renderer, a normal reload opens it again, freezes again,
 * and is killed again -- a loop the user cannot leave because the window never
 * answers long enough to switch chats (#12907). The recovery reload therefore
 * adds `?safe=1`, and this module turns that into one decision for the page:
 * do not auto-open the remembered chat. The user lands on an empty chat
 * surface and picks what to open. The remembered tab strip still shows: a tab
 * is only a label until it is opened, so it loads no history on its own.
 *
 * The flag is read once, before the router mounts, and removed from the address
 * bar right away. A manual refresh after that is a normal load again, and the
 * flag cannot be copied into a shared link. The one exception is a reload the
 * page makes for itself without the user asking (restored UI prefs and the
 * stale-chunk recovery in `main.tsx`, the stale-shell heal in
 * `staleShellHeal.ts`): those go through `reloadKeepingSafe`, so the flag
 * survives into the load that actually renders.
 */

export const SAFE_RELOAD_PARAM = 'safe'

let safeReload = false

/**
 * Read `?safe=1` from the current address and strip it, keeping every other
 * param (the token handshake still needs `?token=`). Call once, before render.
 */
export function captureSafeReload(win: Pick<Window, 'location' | 'history'> = window): boolean {
  try {
    const url = new URL(win.location.href)
    safeReload = url.searchParams.get(SAFE_RELOAD_PARAM) === '1'
    if (url.searchParams.has(SAFE_RELOAD_PARAM)) {
      url.searchParams.delete(SAFE_RELOAD_PARAM)
      win.history.replaceState(win.history.state, '', url.pathname + url.search + url.hash)
    }
  } catch {
    safeReload = false
  }
  return safeReload
}

/** True when this page load came from a crash-recovery reload. */
export function isSafeReload(): boolean {
  return safeReload
}

/**
 * Reload the page from boot code. A safe load stays safe: the address gets
 * `safe=1` back, so the next load still skips the remembered chat. A normal
 * load is a plain reload.
 */
export function reloadKeepingSafe(win: Pick<Window, 'location'> = window): void {
  if (!safeReload) {
    win.location.reload()
    return
  }
  const url = new URL(win.location.href)
  url.searchParams.set(SAFE_RELOAD_PARAM, '1')
  win.location.replace(url.toString())
}

/** Test hook: reset the captured flag. */
export function _resetSafeReloadForTests(): void {
  safeReload = false
}
