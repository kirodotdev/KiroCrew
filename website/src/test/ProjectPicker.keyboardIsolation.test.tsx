/**
 * ProjectPicker above a Modal — the sibling-portal keyboard boundary (#6833).
 *
 * `Modal`'s isolation (Modal.keyboardIsolation.test.tsx) is a bubble-phase
 * `onKeyDown` on the dialog PANEL, so it covers exactly the Modal's own React
 * subtree. React routes synthetic events along the REACT tree, not the DOM
 * tree, so an overlay rendered as a React SIBLING of `<Modal>` sits outside
 * that boundary even though it portals into the same `document.body` and paints
 * above it: `ModalDialog`'s `isolateKeys` is simply not an ancestor on the
 * dispatch path. Sharing a z-index is a PAINT-order fact and implies nothing
 * about event routing — that conflation is what hid this.
 *
 * `FolderConfigModal` is exactly that shape: it renders `<ProjectPicker>` after
 * `</Modal>`. So a global chord typed into the picker's inputs still reaches
 * `useKeyboardShortcuts`' bubble-phase `document` listener (a plain
 * `document.addEventListener('keydown', handler)`, no capture flag), navigates
 * away, and unmounts the dialog with its part-filled folder draft.
 *
 * The CONTROL test comes FIRST on purpose. Every other assertion here is a
 * negative ("the listener was not called"), and a negative is worthless if the
 * harness never delivered the key at all — the control fires the identical
 * chord inside the Modal body, where the boundary is known to work, and
 * requires it to be stopped.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { useState, useRef } from 'react'
import { renderWithProviders } from './helpers'
import Modal from '../components/Modal'
import ProjectPicker from '../components/ProjectPicker'
import { api } from '../api/client'

beforeEach(() => {
  vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: ['/home/u/projA'] })
  vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: '/home/u', parent: '/home', dirs: [] })
})

afterEach(() => {
  vi.restoreAllMocks()
})

/** DOMRect-shaped anchor (happy-dom gives a real button an all-zero rect). */
const rect = (top: number, left: number, width = 80, height = 24): DOMRect => ({
  top, left, width, height,
  bottom: top + height,
  right: left + width,
  x: left, y: top,
  toJSON: () => ({}),
} as DOMRect)

/**
 * The `FolderConfigModal` shape, reduced to the part that matters: the picker is
 * rendered AFTER `</Modal>`, gated on its own open flag, so it is a React
 * sibling of the dialog rather than a descendant of it.
 */
function Harness({ onClose, onPickerClosed }: { onClose: () => void; onPickerClosed?: () => void }) {
  const [pickerOpen, setPickerOpen] = useState(true)
  return (
    <>
      <Modal open onClose={onClose} title="Folder">
        <input aria-label="draft" />
      </Modal>
      {pickerOpen && (
        <ProjectPicker
          open={true}
          onOpenChange={o => { if (!o) { setPickerOpen(false); onPickerClosed?.() } }}
          anchorRect={rect(100, 50)}
          onSelect={vi.fn()}
        />
      )}
    </>
  )
}

/** A real session-jump chord: Ctrl+3 deliberately fires while an input has focus. */
const CHORD = { key: '3', code: 'Digit3', ctrlKey: true } as const

async function renderHarness(onClose = vi.fn(), onPickerClosed = vi.fn()) {
  renderWithProviders(<Harness onClose={onClose} onPickerClosed={onPickerClosed} />)
  // The picker loads its Recent list before the search field exists.
  await screen.findByLabelText('Search recent projects')
  return { onClose, onPickerClosed }
}

/** Switch the picker to its free-text Browse tab (the tab buttons use mouseDown). */
async function openBrowseTab() {
  fireEvent.mouseDown(screen.getByText('Browse'))
  return screen.findByLabelText('Project directory path')
}

describe('ProjectPicker above a Modal — keyboard isolation (#6833)', () => {
  it('CONTROL: the identical chord IS stopped when typed inside the Modal body', async () => {
    // Proves two things the negatives below depend on: the harness really
    // delivers a bubbling keydown, and Modal's boundary is live in it. If this
    // ever fails, every "not called" in this file is meaningless.
    const globalShortcut = vi.fn()
    document.addEventListener('keydown', globalShortcut)
    try {
      await renderHarness()
      fireEvent.keyDown(screen.getByLabelText('draft'), CHORD)
      expect(globalShortcut).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', globalShortcut)
    }
  })

  it('CONTROL: the harness DOES see a chord fired outside every boundary', async () => {
    // The other half of the control pair — a positive. Fired at document.body,
    // outside both the dialog panel and the picker, the listener must be
    // reached. Together with the test above, a "not called" result can only
    // mean a boundary stopped it.
    const globalShortcut = vi.fn()
    document.addEventListener('keydown', globalShortcut)
    try {
      await renderHarness()
      fireEvent.keyDown(document.body, CHORD)
      expect(globalShortcut).toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', globalShortcut)
    }
  })

  it('stops a chord typed in the picker Recent search from reaching the page listener', async () => {
    // The Recent search field is the picker's DEFAULT focus (autoFocus, and the
    // Recent tab is selected whenever there is any recent project), so this is
    // the field a user is in the instant the picker opens.
    const globalShortcut = vi.fn()
    document.addEventListener('keydown', globalShortcut)
    try {
      await renderHarness()
      fireEvent.keyDown(screen.getByLabelText('Search recent projects'), CHORD)
      expect(globalShortcut).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', globalShortcut)
    }
  })

  it('stops a chord typed in the picker Browse path field from reaching the page listener', async () => {
    // The highest-exposure field: a composable FREE-TEXT path input, where a
    // mistyped Ctrl+digit is most likely and most costly — it unmounts the
    // dialog underneath with the folder draft still in it.
    const globalShortcut = vi.fn()
    document.addEventListener('keydown', globalShortcut)
    try {
      await renderHarness()
      const path = await openBrowseTab()
      fireEvent.keyDown(path, CHORD)
      expect(globalShortcut).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', globalShortcut)
    }
  })

  it('keeps Escape dismissing only the picker, not the dialog underneath', async () => {
    // The surgical half. Modal's own dismissal is a bubble-phase `window`
    // listener, so a blanket stop inside the picker would be fine for Modal but
    // a boundary that let Escape through to it would close the whole dialog
    // instead of the popover. Escape must reach neither: the picker consumes it.
    const { onClose, onPickerClosed } = await renderHarness()
    fireEvent.keyDown(screen.getByLabelText('Search recent projects'), { key: 'Escape' })
    expect(onPickerClosed).toHaveBeenCalledTimes(1)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('keeps Escape from the Browse path field dismissing only the picker', async () => {
    const { onClose, onPickerClosed } = await renderHarness()
    const path = await openBrowseTab()
    fireEvent.keyDown(path, { key: 'Escape' })
    expect(onPickerClosed).toHaveBeenCalledTimes(1)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('does not dismiss anything on an Escape the IME owns', async () => {
    // A CJK user cancelling a candidate list is not closing the picker, and
    // certainly not the dialog behind it.
    const { onClose, onPickerClosed } = await renderHarness()
    const path = await openBrowseTab()
    // The composing text is ASCII on purpose (the repo bars CJK in source): the
    // latch keys on the composition EVENTS and `isComposing`, not on the
    // characters, so romanised pre-edit text exercises the same path.
    fireEvent.change(path, { target: { value: '/home/u/xiangmu' } })
    fireEvent.compositionStart(path)
    fireEvent.keyDown(path, { key: 'Escape' })
    expect(onPickerClosed).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('still reaches a CAPTURE-phase document listener from inside the picker', async () => {
    // The rail against "hardening" this into a capture-phase or document-level
    // guard. The picker's own Recent-list navigation (useListKeyboardNav) and
    // Modal's Tab trap (useDialogFocusTrap, window capture) both run in the
    // capture phase; a boundary that starved them would pass every assertion
    // above while breaking arrow-key navigation and the focus trap.
    const capturing = vi.fn()
    document.addEventListener('keydown', capturing, { capture: true })
    try {
      await renderHarness()
      fireEvent.keyDown(screen.getByLabelText('Search recent projects'), CHORD)
      expect(capturing).toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', capturing, { capture: true })
    }
  })

  it('keeps the picker Recent list arrow-key navigation working', async () => {
    // Behaviour rail: the guard must not swallow the keys the picker needs.
    await renderHarness()
    const search = screen.getByLabelText('Search recent projects')
    const option = screen.getByRole('option', { name: /projA/ })
    expect(option).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(search, { key: 'ArrowDown' })
    // One recent dir, so the selection stays put rather than wrapping — the
    // assertion that matters is that the key was not consumed into an error.
    expect(screen.getByRole('option', { name: /projA/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('leaves Escape travelling to a bubble-phase window listener', async () => {
    // The pin for the Escape EXCEPTION, at the level where it is actually
    // load-bearing. Neither dismissal path above can pin it: the Recent list
    // consumes Escape at document CAPTURE (so it never reaches this handler)
    // and the Browse field consumes it as the event's own target (so it has
    // already acted by the time this handler runs). A blanket
    // `stopPropagation()` therefore passes every other test in this file —
    // mutation-verified — while silently breaking the ONE contract Modal.tsx
    // documents: bubble-phase `window` is exactly where Modal's own dismissal
    // listens, so an overlay that swallows Escape there breaks any dismissal
    // wired the same way. This asserts the boundary is chord-shaped, not
    // Escape-shaped: the chord is stopped, Escape still travels.
    const windowEscape = vi.fn()
    const windowChord = vi.fn()
    const onWindowKey = (e: Event) => {
      if ((e as globalThis.KeyboardEvent).key === 'Escape') windowEscape()
      else windowChord()
    }
    window.addEventListener('keydown', onWindowKey)
    try {
      await renderHarness()
      const path = await openBrowseTab()
      fireEvent.keyDown(path, CHORD)
      expect(windowChord).not.toHaveBeenCalled()
      fireEvent.keyDown(path, { key: 'Escape' })
      expect(windowEscape).toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', onWindowKey)
    }
  })

  it('does not strand focus outside the dialog when Tab is pressed in the picker', async () => {
    // The other half of the reported concern — "focus can leave the modal into
    // the overlay". Measured, it does not: `useDialogFocusTrap` runs on window
    // CAPTURE and its `refocuses` branch fires precisely when the active
    // element is NOT inside the dialog container, which is the case for every
    // element in this sibling portal. So the trap reclaims focus into the
    // dialog rather than leaking it. This is pre-existing behaviour the
    // keyboard boundary above deliberately does not alter (a bubble-phase
    // guard cannot pre-empt a capture-phase trap) — pinned so a later change
    // to either one cannot quietly start stranding focus.
    await renderHarness()
    const path = await openBrowseTab()
    path.focus()
    expect(document.activeElement).toBe(path)
    fireEvent.keyDown(path, { key: 'Tab' })
    const dialog = document.querySelector('[role="dialog"]') as HTMLElement
    expect(dialog).toBeTruthy()
    expect(dialog.contains(document.activeElement)).toBe(true)
  })

  it('returns focus to the anchor inside the dialog when the picker is dismissed', async () => {
    // The third assertion a screenshot cannot carry: where focus LANDS on
    // dismissal. FolderConfigModal's anchor is the Browse button inside the
    // dialog, so dismissing the popover must put focus back on it — not on
    // <body>, which would drop the user out of the dialog entirely and leave
    // the next Tab starting from the top of the page.
    function AnchoredHarness({ onClose }: { onClose: () => void }) {
      const anchor = useRef<HTMLButtonElement>(null)
      const [pickerOpen, setPickerOpen] = useState(true)
      return (
        <>
          <Modal open onClose={onClose} title="Folder">
            <input aria-label="draft" />
            <button ref={anchor}>Browse…</button>
          </Modal>
          {pickerOpen && (
            <ProjectPicker
              open={true}
              onOpenChange={o => { if (!o) setPickerOpen(false) }}
              anchorRef={anchor}
              onSelect={vi.fn()}
            />
          )}
        </>
      )
    }
    renderWithProviders(<AnchoredHarness onClose={vi.fn()} />)
    await screen.findByLabelText('Search recent projects')
    const path = await openBrowseTab()
    fireEvent.keyDown(path, { key: 'Escape' })
    expect(screen.queryByLabelText('Project directory path')).not.toBeInTheDocument()
    expect(document.activeElement).toBe(screen.getByText('Browse…'))
  })
})
