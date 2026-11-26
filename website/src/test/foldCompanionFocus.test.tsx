/**
 * Keyboard focus across a book-pose pane allocation change (lib/foldCompanion.ts):
 * the side panel opening hides the sessions list, and closing it brings the list
 * back. Focus inside the pane that leaves moves to the pane that arrives, never to
 * the document body; focus anywhere else stays where it is. Each case runs with
 * the View Transitions API (stubbed to run its update at once) and without it.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { useFoldCompanionLayout } from '../lib/foldCompanion'
import type { ViewportSegment } from '../hooks/useViewportSegments'

const TAB: ViewportSegment[] = [{ x: 0, width: 475.5 }, { x: 475.5, width: 475.5 }]

/** The chat page's panes as the companion layout draws them. */
function Page({ wanted }: { wanted: boolean }) {
  const shown = useFoldCompanionLayout({ wanted, segments: TAB, isMobile: false, publish: false })
  return (
    <div>
      {!shown && (
        <div data-ui-location="sessions.list" data-testid="sessions">
          <div role="button" tabIndex={0} data-testid="row-other">Other</div>
          <div role="button" tabIndex={0} aria-current="true" data-testid="row-active">Active</div>
        </div>
      )}
      <div className="chat-container"><button type="button" data-testid="transcript-control">Copy</button></div>
      <textarea aria-label="Message" data-composer-input="" data-testid="composer" />
      {shown && (
        <div data-vt-panel="" data-testid="panel">
          <button type="button" data-testid="panel-control">Close</button>
        </div>
      )}
    </div>
  )
}

type Doc = Document & { startViewTransition?: (cb: () => void) => { finished: Promise<unknown> } }

describe.each([
  ['with the View Transitions API', true],
  ['without the View Transitions API', false],
] as const)('focus across a pane allocation change, %s', (_name, withApi) => {
  const doc = document as Doc
  const before = doc.startViewTransition
  const install = () => {
    if (withApi) doc.startViewTransition = cb => { cb(); return { finished: Promise.resolve() } }
    else delete doc.startViewTransition
  }
  afterEach(() => {
    cleanup()
    if (before) doc.startViewTransition = before
    else delete doc.startViewTransition
  })

  it('a focused sessions row, then the panel takes the segment: focus moves into the side panel', async () => {
    install()
    const { rerender } = render(<Page wanted={false} />)
    act(() => { screen.getByTestId('row-other').focus() })
    rerender(<Page wanted />)
    await act(async () => {})
    expect(screen.queryByTestId('sessions')).toBeNull()
    expect(document.activeElement).not.toBe(document.body)
    expect(screen.getByTestId('panel').contains(document.activeElement)).toBe(true)
  })

  it('a focused side-panel control, then the panel closes: focus moves to the selected sessions row', async () => {
    install()
    const { rerender } = render(<Page wanted />)
    act(() => { screen.getByTestId('panel-control').focus() })
    rerender(<Page wanted={false} />)
    await act(async () => {})
    expect(screen.queryByTestId('panel')).toBeNull()
    expect(document.activeElement).toBe(screen.getByTestId('row-active'))
  })

  it('a focused transcript control that stays: focus is unchanged either way', async () => {
    install()
    const { rerender } = render(<Page wanted={false} />)
    act(() => { screen.getByTestId('transcript-control').focus() })
    rerender(<Page wanted />)
    await act(async () => {})
    expect(document.activeElement).toBe(screen.getByTestId('transcript-control'))
    rerender(<Page wanted={false} />)
    await act(async () => {})
    expect(document.activeElement).toBe(screen.getByTestId('transcript-control'))
  })
})
