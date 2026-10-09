import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { TOPBAR_FOCUS_Z } from '../lib/themeDecorLayer'

/**
 * App-root dialogs render INSIDE the shell's `relative z-[1]` root (App.tsx),
 * not through Modal.tsx's `document.body` portal, so their stacking order is
 * decided against the shell's own ladder:
 *
 *   z-[59]  sessions flyout        z-[60]  flyout drawer morph
 *   z-[61]  rail toggle / peek     63      mac drag strips
 *   65      workspace fullscreen   67      focus-mode rail + top bar (TOPBAR_FOCUS_Z)
 *   z-[68]  in-shell dialog tier (clears all chat chrome above)
 *   z-[70]  toast / context-menu band
 *   z-[100] full-screen takeovers (update overlay, update-error, update dialogs)
 *
 * An overlay at the chat-pane ceiling `z-50` paints UNDER the flyout stack, so
 * a dialog shown while the chat page is up appears half-hidden behind it. Every
 * App-root dialog's outermost overlay must therefore sit at or above the
 * in-shell dialog tier (`z-[68]`), high enough to clear the flyout, its drawer
 * morph, the rail toggle, workspace fullscreen and the focus-mode chrome at once.
 *
 * Asserted against SOURCE TEXT: the z-indexes are Tailwind classes jsdom cannot
 * resolve into a paint order, and the invariant is the comparison between them.
 */
const SRC = (p: string) => readFileSync(resolve(__dirname, '..', p), 'utf8')

/** The in-shell dialog tier: the floor every App-root dialog must clear to sit
 *  above the chat chrome (flyout z-[59] through the focus-mode chrome). */
const DIALOG_TIER = TOPBAR_FOCUS_Z + 1

/**
 * Each App-root dialog, paired with a regex that pulls the z-index off the
 * outermost overlay this file is pinning. A dialog with more than one overlay
 * (UpdateModal also paints a full-app "installing" surface) lists the regex for
 * the DISMISSABLE dialog overlay — the one the flyout was burying.
 */
const DIALOGS: Record<string, { file: string; overlay: RegExp }> = {
  UpdateModal: {
    file: 'components/UpdateModal.tsx',
    // The dialog overlay (role="button" dismiss scrim), not the sibling
    // full-app install surface which is already a z-[100] takeover.
    overlay: /className="fixed inset-0 z-(?:\[(\d+)\]|(\d+)) bg-bg\/80 backdrop-blur-xs flex items-center justify-center animate-rise"\n\s*role="button"/,
  },
  StartupVideoModal: {
    file: 'components/StartupVideoModal.tsx',
    overlay: /className="fixed inset-0 z-(?:\[(\d+)\]|(\d+)) bg-bg\/80 backdrop-blur-xs flex items-center justify-center"\n\s*role="presentation"/,
  },
  UpdateFoundModal: {
    file: 'components/UpdateFoundModal.tsx',
    overlay: /className="fixed inset-0 z-(?:\[(\d+)\]|(\d+)) bg-bg\/80 backdrop-blur-xs flex items-center justify-center animate-rise"\n\s*role="presentation"/,
  },
}

describe('App-root dialog stacking', () => {
  for (const [name, { file, overlay }] of Object.entries(DIALOGS)) {
    it(`${name}'s overlay sits at or above the in-shell dialog tier`, () => {
      const m = overlay.exec(SRC(file))
      expect(
        m,
        `${name}'s dialog overlay did not match its pinned shape in ${file}`,
      ).not.toBeNull()
      const z = Number(m![1] ?? m![2])
      expect(
        z,
        `${name}'s overlay z-index (${z}) must be >= ${DIALOG_TIER} so it clears the sessions flyout stack`,
      ).toBeGreaterThanOrEqual(DIALOG_TIER)
    })
  }
})
