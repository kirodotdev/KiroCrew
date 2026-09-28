// a11y regression cover for the mochi pinned-file chips (issue #13818).
//
// Two defects the chips shipped with, both invisible to a mouse-free user:
//
//   1. The full path lived only in a native `title` attribute, so a screen
//      reader announced the file's basename with no way to reach the path. The
//      fix gives each chip wrapper `role="group"` + `aria-label={pin.path}`, an
//      accessible name present the moment the chip renders, with no hover.
//   2. The unpin control was rendered only while a pointer-driven `hovered`
//      state was true, so a keyboard user could never reach it and — once it was
//      made keyboard-reachable — a bare red X gave no hint whether it unpinned
//      or deleted. The fix renders the unpin <button> ALWAYS (a real tab stop,
//      hidden by CSS :hover/:focus-within until revealed), uses a pin-off icon
//      instead of a destructive red X, and names it "Unpin <file>" so a
//      keyboard/SR user knows the action and which pin before activating it.
//
// The reveal itself is CSS (`.pin-chip:focus-within .pin-chip-unpin`), which a
// jsdom/happy-dom test does not paint; these assertions therefore pin the DOM
// contract the CSS depends on — the control is present, focusable, and named —
// not the pixel opacity. All fail against the pre-fix component.
//
// PinnedChip is not exported, so we render the exported `PinnedSidePanel`.
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../apps/mochi/src/mochiApi', () => ({
  api: { unpinFile: vi.fn(), markPinnedSeen: vi.fn(), previewFile: vi.fn() },
}))

const { PinnedSidePanel } = await import('../apps/mochi/src/renderer/ChatPanel')

interface PinnedFileEntry {
  path: string
  label: string
  pinnedAt: number
  updatedAt?: number
}

function pin(path: string, over: Partial<PinnedFileEntry> = {}): PinnedFileEntry {
  return { path, label: path.split('/').pop() ?? path, pinnedAt: 1_700_000_000_000, ...over }
}

const renderPanel = (
  pins: PinnedFileEntry[],
  opts: { updated?: string[]; deleted?: string[] } = {},
) =>
  render(
    <PinnedSidePanel
      pins={pins}
      updatedPaths={new Set(opts.updated ?? [])}
      deletedPaths={new Set(opts.deleted ?? [])}
      visible
    />,
  )

afterEach(() => cleanup())

describe('mochi PinnedSidePanel — chip accessibility (#13818)', () => {
  const PATH = '/home/dev/project/src/main.py'
  const NAME = 'main.py'

  it('exposes the full path as an accessible name without any hover (inert chip)', () => {
    // A plain, non-updated pin in a browser tab (isElectron false in jsdom) is
    // the inert branch. Its path is reachable by role name with NO pointer event.
    renderPanel([pin(PATH)])
    expect(screen.getByRole('group', { name: PATH })).toBeTruthy()
  })

  it('renders the unpin button in the DOM without any hover, and it is focusable', () => {
    renderPanel([pin(PATH)])
    // Always present — it is the keyboard tab stop, not a hover-only mount.
    const unpin = screen.getByRole('button')
    expect(unpin).toBeTruthy()
    unpin.focus()
    expect(document.activeElement).toBe(unpin)
  })

  it('names the unpin control with the action AND the file, no hover needed', () => {
    // "Unpin main.py" — self-describing so a keyboard/SR user is not left
    // guessing whether a bare icon unpins or deletes.
    renderPanel([pin(PATH)])
    const unpin = screen.getByRole('button', { name: `Unpin ${NAME}` })
    expect(unpin).toBeTruthy()
  })

  it('carries the accessible name on the clickable chip variant too', () => {
    // An updated pin becomes clickable; that branch is a role="button" wrapper.
    renderPanel([pin(PATH)], { updated: [PATH] })
    expect(screen.getByRole('button', { name: PATH })).toBeTruthy()
    // The unpin control is still present and named on the clickable variant.
    expect(screen.getByRole('button', { name: `Unpin ${NAME}` })).toBeTruthy()
  })

  it('omits the unpin control for a deleted pin', () => {
    // A deleted pin cannot be unpinned via this control; it must not render a
    // dangling button.
    renderPanel([pin(PATH)], { deleted: [PATH] })
    expect(screen.queryByRole('button', { name: `Unpin ${NAME}` })).toBeNull()
  })
})
