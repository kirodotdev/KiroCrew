/**
 * PanelErrorBoundary — the only crash barrier in the Mochi renderer.
 *
 * What is pinned: a render throw becomes the fallback (not a black window), the
 * error is LOGGED rather than swallowed (the shell tails renderer console.error
 * into the main log — that is how a mid-stream crash becomes diagnosable), and
 * Retry remounts the subtree with a bumped key so a component that has since
 * stopped throwing comes back.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { PanelErrorBoundary } from '../panel/PanelErrorBoundary'

/** Throws on first render, then behaves — the "retry actually works" case. */
function Flaky({ throws }: { throws: boolean }) {
  if (throws) throw new Error('zzq boom')
  return <div>zzq recovered</div>
}

let consoleError: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  // React logs the caught error itself; the assertion is about OUR log line, so
  // the channel is spied rather than left to spam the run.
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  consoleError.mockRestore()
})

describe('PanelErrorBoundary', () => {
  it('renders children untouched when nothing throws', () => {
    render(
      <PanelErrorBoundary>
        <div>zzq child</div>
      </PanelErrorBoundary>,
    )
    expect(screen.getByText('zzq child')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows the fallback instead of unwinding to an empty root', () => {
    render(
      <PanelErrorBoundary>
        <Flaky throws />
      </PanelErrorBoundary>,
    )
    expect(screen.getByRole('alert')).toBeTruthy()
    expect(screen.getByText('Something went wrong')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Reload panel' })).toBeTruthy()
  })

  it('logs the caught error rather than hiding it', () => {
    render(
      <PanelErrorBoundary>
        <Flaky throws />
      </PanelErrorBoundary>,
    )
    const ours = consoleError.mock.calls.find(
      (c) => typeof c[0] === 'string' && c[0].includes('[mochi] panel render crashed:'),
    )
    expect(ours).toBeTruthy()
    expect((ours?.[1] as Error).message).toBe('zzq boom')
  })

  it('remounts the subtree on Retry, so a child that stopped throwing comes back', () => {
    function Host() {
      // The boundary clears its error on retry; the child stops throwing because
      // the outer state flipped — together that is the recovery path.
      return <Flaky throws={false} />
    }
    const { rerender } = render(
      <PanelErrorBoundary>
        <Flaky throws />
      </PanelErrorBoundary>,
    )
    expect(screen.getByRole('alert')).toBeTruthy()

    rerender(
      <PanelErrorBoundary>
        <Host />
      </PanelErrorBoundary>,
    )
    // Still the fallback: clearing the error is Retry's job, not a re-render's.
    expect(screen.getByRole('alert')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Reload panel' }))
    expect(screen.getByText('zzq recovered')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
