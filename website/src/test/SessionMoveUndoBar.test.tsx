/**
 * Drag-move undo bar contract.
 *
 * The bar is the only thing standing between a mis-aimed drag and a session
 * silently filed somewhere the user never chose, so what is locked here is
 * (1) it names the destination, (2) both ways of firing undo work, and
 * (3) the keyboard path does NOT steal the chord from a text field — the
 * composer implements its own undo history and a hijack there would revert a
 * folder move while the user was only un-typing a word.
 *
 * framer-motion is rendered as plain DOM (jsdom cannot RUN its animations), so
 * the countdown is pinned by the values it is driven with rather than by pixels:
 * scaleX must always be `remaining / MOVE_UNDO_MS`, which is what makes a
 * suspended deadline stop draining on screen. The 8s DEADLINE itself is not this
 * component's — ChatSidebar owns it, because an offer whose optimistic move
 * never became visible has no bar to run a timer and must still expire; see
 * ChatSidebar.moveUndo.test.tsx.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set(['initial', 'animate', 'exit', 'transition', 'layout', 'layoutId'])
  // jsdom cannot RUN the animations, but the countdown's correctness is entirely
  // in the values handed to them, so the two that carry it are surfaced as data
  // attributes rather than dropped on the floor.
  const SURFACED = new Set(['animate', 'transition'])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>((props, ref) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (FRAMER_PROPS.has(k)) {
          if (SURFACED.has(k)) clean[`data-framer-${k}`] = JSON.stringify(props[k])
          continue
        }
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  return { motion: new Proxy({}, { get: (_t, tag: string) => make(tag) }) }
})

import SessionMoveUndoBar, { MOVE_UNDO_MS, type MovedSession } from '../components/SessionMoveUndoBar'

const moved: MovedSession = {
  slotKey: 'chat-1',
  fromFolderId: null,
  toFolderId: 'f-archive',
  toFolderName: 'Archive',
  sessionTitle: 'Session drag lands in the wrong folder',
}

function renderBar(over: Partial<MovedSession> = {}, handlers: { onUndo?: () => void } = {}) {
  const onUndo = handlers.onUndo ?? vi.fn()
  const utils = render(<SessionMoveUndoBar moved={{ ...moved, ...over }} onUndo={onUndo} />)
  return { ...utils, onUndo }
}

describe('SessionMoveUndoBar', () => {
  it('names the destination folder', () => {
    renderBar()
    expect(screen.getByText('Archive')).toBeTruthy()
    expect(screen.getByText('Moved to')).toBeTruthy()
  })

  it('gives the root case its own sentence in the sidebar\u2019s own vocabulary', () => {
    // Not "Moved to Unfiled": the sidebar calls this "remove from folder" on its
    // own drop zone, and "Unfiled" is Artifacts vocabulary this surface never shows.
    renderBar({ toFolderId: null, toFolderName: null })
    expect(screen.getByText('Removed from folder')).toBeTruthy()
    expect(screen.queryByText('Moved to')).toBeNull()
  })

  it('reports hold state so the owner can suspend the expiry deadline', () => {
    const onHoldChange = vi.fn()
    const { container } = render(
      <SessionMoveUndoBar moved={moved} onUndo={vi.fn()} onHoldChange={onHoldChange} />,
    )
    const bar = container.querySelector('[data-testid="session-move-undo"]')!
    fireEvent.mouseEnter(bar)
    expect(onHoldChange).toHaveBeenLastCalledWith(true)
    fireEvent.mouseLeave(bar)
    expect(onHoldChange).toHaveBeenLastCalledWith(false)
    fireEvent.focus(screen.getByTestId('session-move-undo-button'))
    expect(onHoldChange).toHaveBeenLastCalledWith(true)
  })

  it('carries the session title as the row tooltip, since the row has no width for it', () => {
    const { container } = renderBar()
    expect(container.querySelector(`[title*="${moved.sessionTitle}"]`)).toBeTruthy()
  })

  // ── Narrow sidebar (down to SIDEBAR_MIN = 180px) ───────────────────────────
  // The destination is the one thing this bar exists to say, so it must be the
  // LAST thing to go when the row runs out of room — not the first.

  it('keeps the destination and drops the prefix when compact', () => {
    const { container } = render(
      <SessionMoveUndoBar moved={moved} onUndo={vi.fn()} compact />,
    )
    expect(screen.getByText('Archive')).toBeTruthy()
    expect(screen.queryByText('Moved to')).toBeNull()
    // …and the full sentence is still reachable on hover.
    expect(container.querySelector('[title*="Moved to Archive"]')).toBeTruthy()
  })

  it('still fires the chord when compact', () => {
    const onUndo = vi.fn()
    render(<SessionMoveUndoBar moved={moved} onUndo={onUndo} compact />)
    fireEvent.keyDown(window, { key: 'z', ctrlKey: true })
    expect(onUndo).toHaveBeenCalledTimes(1)
  })

  it('announces itself as a live status region', () => {
    renderBar()
    const status = screen.getByRole('status')
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(status.textContent).toContain('Archive')
  })

  it('undoes on click', () => {
    const { onUndo } = renderBar()
    fireEvent.click(screen.getByTestId('session-move-undo-button'))
    expect(onUndo).toHaveBeenCalledTimes(1)
  })

  it('labels the button "Undo" and nothing else — the chord is not part of the face', () => {
    renderBar()
    const btn = screen.getByTestId('session-move-undo-button')
    expect(btn.textContent?.trim()).toBe('Undo')
    // The shortcut stays DISCOVERABLE without being decoration on the face.
    expect(btn.getAttribute('title')).toMatch(/Undo\s+(Ctrl\+Z|⌘Z)/)
    expect(btn.getAttribute('aria-keyshortcuts')).toBe('Control+Z')
  })

  it('undoes on the platform undo chord', () => {
    const { onUndo } = renderBar()
    // jsdom reports a non-Mac platform, so the bound chord is Ctrl+Z.
    fireEvent.keyDown(window, { key: 'z', ctrlKey: true })
    expect(onUndo).toHaveBeenCalledTimes(1)
  })

  it('leaves the chord alone while focus is in a text field', () => {
    const { onUndo } = renderBar()
    const input = document.createElement('input')
    document.body.appendChild(input)
    input.focus()
    fireEvent.keyDown(input, { key: 'z', ctrlKey: true })
    expect(onUndo).not.toHaveBeenCalled()
    input.remove()
  })

  it('ignores redo (shift) and a bare z', () => {
    const { onUndo } = renderBar()
    fireEvent.keyDown(window, { key: 'z', ctrlKey: true, shiftKey: true })
    fireEvent.keyDown(window, { key: 'z' })
    expect(onUndo).not.toHaveBeenCalled()
  })

  it('stops listening for the chord once unmounted', () => {
    const { onUndo, unmount } = renderBar()
    unmount()
    fireEvent.keyDown(window, { key: 'z', ctrlKey: true })
    expect(onUndo).not.toHaveBeenCalled()
  })
})

describe('SessionMoveUndoBar on macOS', () => {
  beforeEach(() => vi.resetModules())

  it('binds ⌘Z, not Ctrl+Z', async () => {
    vi.doMock('../utils/platform', () => ({
      isMac: true,
      platformShortcut: (s: string) => s.replace(/Cmd\+/g, '⌘'),
    }))
    const { default: MacBar } = await import('../components/SessionMoveUndoBar')
    const onUndo = vi.fn()
    render(<MacBar moved={moved} onUndo={onUndo} />)
    // The Mac chord is advertised in the tooltip, not on the face…
    const btn = screen.getByTestId('session-move-undo-button')
    expect(btn.textContent?.trim()).toBe('Undo')
    expect(btn.getAttribute('title')).toContain('⌘Z')
    expect(btn.getAttribute('aria-keyshortcuts')).toBe('Meta+Z')
    // …Ctrl+Z is NOT it on this platform…
    fireEvent.keyDown(window, { key: 'z', ctrlKey: true })
    expect(onUndo).not.toHaveBeenCalled()
    // …and ⌘Z is.
    fireEvent.keyDown(window, { key: 'z', metaKey: true })
    expect(onUndo).toHaveBeenCalledTimes(1)
    vi.doUnmock('../utils/platform')
  })

  it('draws the owner’s remainder, and freezes rather than draining while paused', () => {
    const onUndo = vi.fn()
    const countdown = () => screen.getByTestId('session-move-undo-countdown')
    const animate = () => JSON.parse(countdown().getAttribute('data-framer-animate')!)
    const transition = () => JSON.parse(countdown().getAttribute('data-framer-transition')!)

    // Running: head for empty over exactly the time the owner says is left — not
    // over a fixed MOVE_UNDO_MS measured from this component's own mount.
    const { rerender } = render(
      <SessionMoveUndoBar moved={moved} onUndo={onUndo} remainingMs={3000} />,
    )
    expect(animate().scaleX).toBe(0)
    expect(transition().duration).toBe(3)

    // Paused: hold the width at the remaining FRACTION. Continuing to 0 here is
    // the defect — the bar would empty while the deadline was suspended, telling
    // the user the offer expired at the exact moment it is guaranteed alive.
    rerender(<SessionMoveUndoBar moved={moved} onUndo={onUndo} remainingMs={3000} paused />)
    expect(animate().scaleX).toBeCloseTo(3000 / MOVE_UNDO_MS)
    expect(transition().duration).toBe(0)

    // Resumed: pick up from that fraction over the same remainder, so scaleX
    // stays equal to remaining / MOVE_UNDO_MS at every instant.
    rerender(<SessionMoveUndoBar moved={moved} onUndo={onUndo} remainingMs={3000} />)
    expect(animate().scaleX).toBe(0)
    expect(transition().duration).toBe(3)
  })
})
