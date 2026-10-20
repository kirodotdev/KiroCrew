/**
 * Every document- or window-level key handler of a layer rendered through
 * `OverlayPortal`, and whether it stands down while the local Crew is hidden.
 *
 * Hiding the local Crew's overlay root stops painting, not listening
 * (components/overlayRoot.tsx, website/docs/frontend-conventions.md "Floating
 * layers and Crew switches"). A layer's own JSX `onKeyDown` cannot fire while
 * hidden, since nothing under `display:none` holds focus, but a listener on
 * `document` or `window` still hears keys pressed on the remote Crew in front.
 * So each such file either reads the hidden state (`useLocalCrewHidden`,
 * `standDownWhileHidden`), closes on a switch (`useCloseWhenCrewHidden`, so a
 * listener registered only while open is gone), or is listed below with the
 * reason its listener is harmless while hidden. A new layer with a key listener
 * fails here until it is classified. A first-run chapter renders its whole body
 * through `OnboardingChapterShell`, which portals, so importing that shell
 * counts as rendering a layer.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const SRC = join(__dirname, '..')

function* walk(dir: string): Generator<string> {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) { yield* walk(p); continue }
    if (/\.(ts|tsx)$/.test(name) && !/\.(test|stories)\./.test(name)) yield p
  }
}

const IMPORTS_OVERLAY_ROOT = /from\s+['"][^'"]*\/(?:overlayRoot|OnboardingChapterShell)['"]/
const GLOBAL_KEY_LISTENER = /\b(?:document|window)\.addEventListener\(\s*['"]key(?:down|up)['"]/
const GUARDED = /\b(?:useLocalCrewHidden|standDownWhileHidden|useCloseWhenCrewHidden)\b/

/** Listener files that need no guard, and why. Paths are relative to src/. */
const EXEMPT: Record<string, string> = {
  'components/AgentSelector.tsx':
    'claims Escape only when its target is inside the popup or its trigger, which a key on the Crew in front never is',
  'components/CommandPalette.tsx':
    'mounted by QuickSearchSurface outside the local pane, so its portal is document.body and it is never hidden',
  'components/InstantTip.tsx': 'Escape only dismisses the tooltip; it does not prevent or stop the key',
  'components/MemoryModeChip.tsx': 'Escape only closes its own popover; it does not prevent or stop the key',
  'components/PastedChip.tsx': 'Escape only closes its hover preview; it does not prevent or stop the key',
  'components/PasteHoverLayer.tsx': 'Escape only closes its hover preview; it does not prevent or stop the key',
  'guide/GuideLayer.tsx': 'the listener only records that the last input was a key, for focus handling',
  'pages/chat/McpInfoButton.tsx': 'Escape only closes its own hover card; it does not prevent or stop the key',
  'pages/connections/ConnectionsPageCard.tsx': 'Escape only unpins its own tip; it does not prevent or stop the key',
}

const layerKeyListeners = () => {
  const out: { file: string; guarded: boolean }[] = []
  for (const p of walk(SRC)) {
    const src = readFileSync(p, 'utf8')
    if (!IMPORTS_OVERLAY_ROOT.test(src) || !GLOBAL_KEY_LISTENER.test(src)) continue
    out.push({ file: relative(SRC, p).split('\\').join('/'), guarded: GUARDED.test(src) })
  }
  return out
}

describe('overlay key listeners while the local Crew is hidden', () => {
  const found = layerKeyListeners()

  it('finds the layers it is meant to check', () => {
    const files = found.map(f => f.file)
    for (const known of ['components/Modal.tsx', 'hooks/useDialogFocusTrap.ts', 'components/markdown/Lightbox.tsx', 'components/AgentImportFlow.tsx']) {
      expect(files, known).toContain(known)
    }
  })

  it('guards every unlisted layer key listener', () => {
    const unguarded = found.filter(f => !f.guarded && !(f.file in EXEMPT)).map(f => f.file)
    expect(unguarded, 'read useLocalCrewHidden (or close on a switch), or list the file in EXEMPT with a reason').toEqual([])
  })

  it('lists only files that still need the exemption', () => {
    const live = new Map(found.map(f => [f.file, f.guarded]))
    for (const file of Object.keys(EXEMPT)) {
      expect(live.has(file), `${file} no longer has a layer key listener`).toBe(true)
      expect(live.get(file), `${file} is now guarded; drop it from EXEMPT`).toBe(false)
    }
  })
})
