import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * The header (with the pinned prompt), the composer dock and, on a phone, the
 * shell's top bar (which hosts the session title) all overlay the transcript.
 * A touch selection handle dragged onto one of them must stay in the
 * transcript: the overlays are made `inert` while a selection is held, and the
 * page puts an endpoint that leaves its row back where it last sat.
 */
// ChatPage composes owners under pages/chat/page/ (and its useChatPage* siblings), so
// the pins read the page and those owners.
const PAGES_DIR = resolve(__dirname, '../pages')
const OWNER_DIR = resolve(PAGES_DIR, 'chat/page')
const page = [
  readFileSync(resolve(PAGES_DIR, 'ChatPage.tsx'), 'utf8'),
  ...readdirSync(OWNER_DIR).filter(f => /\.tsx?$/.test(f) && !/\.test\./.test(f)).sort()
    .map(f => readFileSync(resolve(OWNER_DIR, f), 'utf8')),
  ...readdirSync(PAGES_DIR).filter(f => /^useChatPage.*\.tsx?$/.test(f) && !/\.test\./.test(f)).sort()
    .map(f => readFileSync(resolve(PAGES_DIR, f), 'utf8')),
].join('\n')
const app = ['App.tsx', 'shell/appShell.tsx', 'shell/topbar/shellHeader.tsx', 'shell/nav/navRail.tsx', 'shell/nav/navRow.tsx'].map(f => readFileSync(resolve(__dirname, '..', f), 'utf8')).join('\n')

describe('transcript overlays do not take a touch selection', () => {
  it('marks the header overlay and the composer dock for selection-time inert', () => {
    expect(page).toMatch(/absolute top-0 left-0 right-1\.5 [^`]*pointer-events-none`\} style=\{[^}]*\}[^>]*\[SELECTION_INERT_ATTR\]/)
    expect(page).toMatch(/data-testid="composer-dock-root" \{\.\.\.\{ \[SELECTION_INERT_ATTR\]: '' \}\}/)
    expect(page).toMatch(/useSelectionInertOverlays\(scrollerRef\)/)
  })
  it('marks the shell top bar for selection-time inert', () => {
    expect(app).toMatch(/className=\{`topbar topbar-glass[^`]*`\}[\s\S]{0,400}?\{\.\.\.\{ \[SELECTION_INERT_ATTR\]: '' \}\}/)
  })
  it('wires the transcript selection to the virtualizer retained range', () => {
    expect(page).toMatch(/const \{ retainRange[^}]*\} = virt/)
    expect(page).toMatch(/addEventListener\('selectionchange', syncSelectionRetention\)/)
    expect(page).toMatch(/nextRetainedRange\(/)
    // An endpoint on no row must not release the retained span.
    expect(page).not.toMatch(/else retainRange\(null\)/)
  })
  it('restores only a selection that has already sat on rows', () => {
    // Rewriting a fresh long-press broke the first Android selection.
    expect(page).toMatch(/restoreEndpointToTranscript\(scroller, selection, lastOnRows\)/)
    expect(page).not.toMatch(/setBaseAndExtent/)
  })
})
