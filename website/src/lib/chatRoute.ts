/** The chat route, spelled once. Several things key off it, among them the
 *  phone's single-bar header variant, the shell nav drawer's swipe gate and the
 *  fold's two-pane form (lib/touchLayoutForm.ts); a drift between two
 *  spellings is exactly how "two drawers for one gesture" would come back. Kept in lib so code outside the shell reads it without importing a
 *  shell owner; shell/nav/routeActive.ts re-exports it. */
export const isChatRoute = (pathname: string) => pathname === '/chat' || pathname.startsWith('/chat/') || pathname === '/'
