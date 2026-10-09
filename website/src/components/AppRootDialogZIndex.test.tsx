/**
 * App-root dialog stacking.
 *
 * The App shell mounts several full-screen dialogs as siblings inside its own
 * `relative z-[1]` root (App.tsx) — NOT portaled to document.body the way
 * Modal.tsx is. That means their z-index competes directly with the chat page's
 * own chrome, which deliberately paints ABOVE the chat pane: the sessions flyout
 * (`SessionFlyout.tsx`, z-[59]) and its drawer morph (z-[60]), the focus-peek
 * rail toggle (App.tsx, z-[61]), and the focus-mode top chrome / mac drag strips
 * (inline zIndex 63, and TOPBAR_FOCUS_Z for the focus-mode rail and top bar). A dialog overlay left on the chat-pane ceiling (z-50)
 * paints UNDER all of that — the bug #15754 fixed for MobileConnectModal and
 * #15776 fixes for the three remaining App-root dialogs.
 *
 * This test pins that contract at the SOURCE level, the same technique
 * `MobileConnectModal.test.tsx` uses: jsdom paints nothing, so the only sound
 * thing to compare is the z-index each source declares, which is the property
 * paint order follows. Two invariants per dialog overlay:
 *
 *   1. it sits ABOVE every chat-chrome layer, so it can no longer be obscured
 *      by the sessions flyout and friends; and
 *   2. it stays strictly BELOW the shell's z-[100] full-screen takeover layer
 *      (UpdateModal's "Installing update…" / install-failed surface, and
 *      App.tsx's update-error and first-run handoff overlays), which must keep
 *      covering everything — including a dialog that happens to be open —
 *      because they render in the SAME shell stacking context DOM-earlier, so a
 *      tie at z-[100] here would win on document order and paint a dismissible
 *      dialog over the surface meant to hide the app mid-update.
 *
 * Modal.tsx's own z-[100] is explicitly NOT the bar: Modal portals to
 * document.body, a separate stacking context where z-[100] is correct. These
 * dialogs do not, so the right layer for them is the band between the chat
 * chrome (max TOPBAR_FOCUS_Z) and the shell takeover layer (100) — z-[68] today.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it, expect } from 'vitest'
import { TOPBAR_FOCUS_Z } from '../lib/themeDecorLayer'

const SRC = join(__dirname, '..')
const read = (...parts: string[]) => readFileSync(join(SRC, ...parts), 'utf8')

/** Every `z-[N]` / `z-N` Tailwind class in a blob, as numbers. */
const zClasses = (src: string): number[] =>
  [...src.matchAll(/\bz-(?:\[(\d+)\]|(\d+))(?![\w-])/g)].map(m => Number(m[1] ?? m[2]))

/** The z of each `fixed inset-0 z-[N]` overlay a source declares. */
const fixedOverlayZs = (src: string): number[] =>
  [...src.matchAll(/fixed inset-0 z-\[(\d+)\]/g)].map(m => Number(m[1]))

/**
 * The shell's full-screen takeover layer. The shell paints its whole-app
 * takeovers at z-[100] (UpdateModal's installing/failed surface, App.tsx's
 * update-error and first-run handoff overlays, and Modal.tsx's portaled layer
 * all use it), so a dismissible App-root dialog must stay strictly below it.
 * Pinned as a literal AND verified present below, so the constant cannot drift
 * away from the sources without turning this red.
 */
const SHELL_TAKEOVER_Z = 100

/**
 * The highest z-index of the chat-page chrome that paints above the chat pane.
 * A dialog overlay must clear ALL of it. Read from the real sources so the bar
 * tracks the chrome rather than a copied constant going stale.
 */
function chatChromeCeiling(): number {
  const flyout = zClasses(read('pages', 'chat', 'SessionFlyout.tsx'))
  const app = read('App.tsx')
  // The focus-peek layers (rail toggle included) are z-[N] classes.
  const peek = app
    .split('\n')
    .filter(line => line.includes('focus-peek-'))
    .flatMap(zClasses)
  // The focus-mode top chrome and mac drag strips are INLINE zIndex style
  // props, invisible to the class scan — read them directly and fail loudly if
  // the shell stops declaring them, so a chrome bump cannot silently outrank a
  // dialog without turning this red. Bounded below the takeover layer so a
  // stray large inline z (e.g. a portaled 9999) cannot poison the ceiling.
  const inlineZ = [...app.matchAll(/zIndex:\s*(\d+)/g)]
    .map(m => Number(m[1]))
    .filter(z => z < SHELL_TAKEOVER_Z)
  // The focus-mode rail and top bar name their layer by constant, which the
  // literal scan above cannot see.
  if (/zIndex:\s*TOPBAR_FOCUS_Z\b/.test(app)) inlineZ.push(TOPBAR_FOCUS_Z)
  expect(flyout.length).toBeGreaterThan(0)
  expect(peek.length).toBeGreaterThan(0)
  expect(inlineZ.length).toBeGreaterThan(0)
  return Math.max(...flyout, ...peek, ...inlineZ)
}

// The three App-root dialogs #15776 moves off z-50. Each returns its overlay
// as one or more `fixed inset-0 z-[N]` divs rendered inside the App shell root.
const APP_ROOT_DIALOGS: Array<{ name: string; file: string }> = [
  { name: 'UpdateModal', file: 'UpdateModal.tsx' },
  { name: 'StartupVideoModal', file: 'StartupVideoModal.tsx' },
  { name: 'UpdateFoundModal', file: 'UpdateFoundModal.tsx' },
]

describe('App-root dialogs paint above the chat chrome, below the takeovers', () => {
  const ceiling = chatChromeCeiling()

  it('the shell declares its full-screen takeover at z-[100]', () => {
    // The constant above is only sound while the sources still use it. Both
    // UpdateModal (installing/failed) and App.tsx (update-error, handoff) must
    // declare a z-[100] takeover; if they move, this fails and the floor is
    // re-pinned deliberately rather than silently.
    expect(fixedOverlayZs(read('components', 'UpdateModal.tsx'))).toContain(SHELL_TAKEOVER_Z)
    expect(fixedOverlayZs(read('App.tsx'))).toContain(SHELL_TAKEOVER_Z)
  })

  it('leaves a z band between the chat chrome and the takeover layer', () => {
    expect(ceiling).toBeLessThan(SHELL_TAKEOVER_Z)
  })

  it.each(APP_ROOT_DIALOGS)('$name has at least one dismissible dialog overlay in the band', ({ file }) => {
    const dialogOverlays = fixedOverlayZs(read('components', file)).filter(z => z < SHELL_TAKEOVER_Z)
    expect(dialogOverlays.length).toBeGreaterThan(0)
  })

  it.each(APP_ROOT_DIALOGS)('$name dismissible overlay sits above the chat chrome', ({ file }) => {
    // The dismissible dialog overlay is the one below the takeover layer; a
    // full-screen takeover (UpdateModal only) sits AT z-[100] and is excluded.
    const dialogOverlays = fixedOverlayZs(read('components', file)).filter(z => z < SHELL_TAKEOVER_Z)
    for (const z of dialogOverlays) expect(z).toBeGreaterThan(ceiling)
  })

  it.each(APP_ROOT_DIALOGS)('$name never paints a dismissible overlay at or above the takeover layer', ({ file }) => {
    // Every overlay is EITHER a dismissible dialog (strictly below 100) OR a
    // deliberate full-screen takeover (exactly 100). Nothing in between, and
    // nothing above — a dialog at 100 would tie the DOM-earlier takeovers.
    for (const z of fixedOverlayZs(read('components', file))) {
      expect(z).toBeLessThanOrEqual(SHELL_TAKEOVER_Z)
    }
  })

  it('UpdateModal keeps its full-screen takeover at the shell takeover layer', () => {
    // The "Installing update…" surface deliberately covers the whole app, so
    // UpdateModal must still declare an overlay AT z-[100]; moving every
    // overlay down to the dialog band would let the app's dying surfaces show
    // through mid-install.
    expect(fixedOverlayZs(read('components', 'UpdateModal.tsx'))).toContain(SHELL_TAKEOVER_Z)
  })
})
