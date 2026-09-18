import { createPortal } from 'react-dom'
import { useEffect, useRef, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { WorkspaceFullscreenContext } from '../components/WorkspacePanelContext'
import { useWorkspaceFullscreenPanel } from './useWorkspaceFullscreenPanel'

function Panel({ mounted, terminalSaw }: { mounted: () => void; terminalSaw?: (defaultPrevented: boolean) => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const { fullscreen, onKeyDown } = useWorkspaceFullscreenPanel(ref)
  useEffect(mounted, [mounted])
  const xterm = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Escape') return
    terminalSaw?.(event.nativeEvent.defaultPrevented)
    event.preventDefault()
    event.stopPropagation()
  }
  // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- fullscreen Escape boundary.
  return <div id="test-workspace" ref={ref} role="group" aria-label="workspace" tabIndex={-1} onKeyDown={onKeyDown} data-testid="panel" data-fullscreen={fullscreen}>
    <input aria-label="draft" defaultValue="unsaved" />
    <textarea aria-label="terminal" onKeyDown={xterm} />
    <input aria-label="find" onKeyDown={event => { if (event.key === 'Escape') event.stopPropagation() }} />
  </div>
}

const noop = () => {}
function Harness({ mounted = noop, terminalSaw }: { mounted?: () => void; terminalSaw?: (defaultPrevented: boolean) => void }) {
  const [fullscreen, setFullscreen] = useState(false)
  // Mirrors the dashboard: the topbar, the panel's host slot and the content
  // grid holding the chat are siblings under the shell. Fullscreen covers the
  // content grid, not the topbar.
  return <div data-testid="dashboard-shell">
    <header data-testid="topbar"><button>Search</button></header>
    <div data-testid="content-grid">
      <div data-testid="chrome"><button>Chat</button></div>
      <button onClick={() => setFullscreen(value => !value)}>Toggle</button>
    </div>
    <div id="activity-bar-slot">
      <WorkspaceFullscreenContext.Provider value={{ fullscreen, exit: () => setFullscreen(false), toggle: () => setFullscreen(value => !value) }}>
        <Panel mounted={mounted} terminalSaw={terminalSaw} />
      </WorkspaceFullscreenContext.Provider>
    </div>
  </div>
}

describe('workspace fullscreen continuity and keyboard ownership', () => {
  it('retains the panel and draft while restoring chrome after Escape', () => {
    const mounted = vi.fn()
    render(<Harness mounted={mounted} />)
    const panel = screen.getByTestId('panel')
    const draft = screen.getByLabelText('draft')
    fireEvent.change(draft, { target: { value: 'edited' } })
    fireEvent.click(screen.getByText('Toggle'))
    expect(panel).toHaveAttribute('data-fullscreen', 'true')
    // The covered content grid (chat, rail) leaves keyboard navigation ...
    expect(screen.getByTestId('content-grid').inert).toBe(true)
    // ... while the topbar above the covered rows keeps taking input.
    expect(screen.getByTestId('topbar').inert).toBeFalsy()
    fireEvent.keyDown(draft, { key: 'Escape' })
    expect(panel).toHaveAttribute('data-fullscreen', 'false')
    expect(screen.getByLabelText('draft')).toBe(draft)
    expect(draft).toHaveValue('edited')
    expect(screen.getByTestId('content-grid').inert).toBeFalsy()
    expect(mounted).toHaveBeenCalledTimes(1)
  })

  it('leaves Escape to a focused terminal and keeps fullscreen', () => {
    const terminalSaw = vi.fn()
    render(<Harness terminalSaw={terminalSaw} />)
    fireEvent.click(screen.getByText('Toggle'))
    const panel = screen.getByTestId('panel')
    fireEvent.keyDown(screen.getByLabelText('terminal'), { key: 'Escape' })
    expect(terminalSaw).toHaveBeenCalledWith(false)
    expect(panel).toHaveAttribute('data-fullscreen', 'true')
  })

  it('yields to nested controls and dialogs before exiting', () => {
    render(<Harness />)
    fireEvent.click(screen.getByText('Toggle'))
    const panel = screen.getByTestId('panel')
    fireEvent.keyDown(screen.getByLabelText('find'), { key: 'Escape' })
    expect(panel).toHaveAttribute('data-fullscreen', 'true')
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    document.body.append(dialog)
    try {
      fireEvent.keyDown(screen.getByLabelText('draft'), { key: 'Escape' })
      expect(panel).toHaveAttribute('data-fullscreen', 'true')
    } finally {
      dialog.remove()
    }
    fireEvent.keyDown(screen.getByLabelText('draft'), { key: 'Escape' })
    expect(panel).toHaveAttribute('data-fullscreen', 'false')
  })

  it('yields to a workspace-owned body portal', () => {
    function Annotation() {
      const [open, setOpen] = useState(true)
      useEffect(() => {
        const onKeyUp = (event: globalThis.KeyboardEvent) => {
          if (event.key === 'Escape') setOpen(false)
        }
        document.addEventListener('keyup', onKeyUp)
        return () => document.removeEventListener('keyup', onKeyUp)
      }, [])
      return open ? createPortal(
        <div data-workspace-escape-owner="test-workspace"><textarea aria-label="annotation draft" defaultValue="Keep this draft" /></div>,
        document.body,
      ) : null
    }
    render(<Harness />)
    fireEvent.click(screen.getByText('Toggle'))
    render(<Annotation />)
    const panel = screen.getByTestId('panel')
    const draft = screen.getByLabelText('draft')
    fireEvent.keyDown(draft, { key: 'Escape' })
    expect(panel).toHaveAttribute('data-fullscreen', 'true')
    fireEvent.keyUp(draft, { key: 'Escape' })
    expect(screen.queryByLabelText('annotation draft')).not.toBeInTheDocument()
    fireEvent.keyDown(draft, { key: 'Escape' })
    expect(panel).toHaveAttribute('data-fullscreen', 'false')
  })
})
