/**
 * The look preview: the first-run "Pick your look" step shows the REAL
 * dashboard, loaded in a same-origin iframe at `/chat?look-preview=1` and
 * scaled down, so the mode, the color theme and the Translucent panels switch
 * are previewed on the product itself rather than on a drawing of it.
 *
 * Inside that frame the app runs as a PASSIVE MIRROR of the parent document:
 * - nothing opens over it: `App.tsx` mounts every self-opening launch surface
 *   (the first-run chapters, the startup clip, the update and changelog
 *   dialogs, the mobile-connect dialog) from ONE block that the frame leaves
 *   out, pinned by `test/App.lookPreviewFrame.test.tsx`;
 * - it follows the parent's look through `storage` events on the three
 *   browser-local keys the parent writes as the user picks (`mc-theme`,
 *   `mc-color-theme` in `hooks/useTheme.tsx`; `mc-liquid-glass` in
 *   `lookPreviewBoot.ts`). Outside the frame those listeners are not
 *   installed: ordinary tabs keep today's behaviour (the gateway is the source
 *   of truth for the theme; a second tab converges on its next boot).
 * - it shows one demo session from fixtures (`lookPreviewFixtures.ts`) and
 *   drops every socket frame unread;
 * - it cannot change anything the parent or the gateway owns: `lookPreviewBoot.ts`
 *   fences its `localStorage` and `sessionStorage` (reads fall through, writes
 *   stay in the frame), answers every non-GET/HEAD `fetch` locally, silences
 *   `BroadcastChannel` and `WebSocket#send`.
 * The parent renders the frame `aria-hidden`, `inert` and `pointer-events:
 * none`, so nothing in it is reachable.
 */
export const LOOK_PREVIEW_PARAM = 'look-preview'

export function lookPreviewSrc(): string {
  return `/chat?${LOOK_PREVIEW_PARAM}=1`
}

/** Is THIS document the preview frame? Read once per call from the URL. */
export function isLookPreviewFrame(): boolean {
  if (typeof location === 'undefined') return false
  try {
    return new URLSearchParams(location.search).has(LOOK_PREVIEW_PARAM)
  } catch {
    return false
  }
}
