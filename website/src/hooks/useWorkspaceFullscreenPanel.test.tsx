import { createPortal } from 'react-dom'
import { useRef, useState, useEffect } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { WorkspaceFullscreenContext } from '../components/WorkspacePanelContext'
import { useWorkspaceFullscreenPanel } from './useWorkspaceFullscreenPanel'

function Panel({ mounted, terminalSaw }: { mounted: () => void; terminalSaw?: (defaultPrevented: boolean) => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const { fullscreen, onKeyDown } = useWorkspaceFullscreenPanel(ref)
  useEffect(mounted, [mounted])
  // xterm's own keydown handling: it records whether the key reached it intact,
  // then consumes Escape for the PTY exactly as the real terminal does.
  const xterm = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Escape') return
    terminalSaw?.(event.nativeEvent.defaultPrevented)
    event.preventDefault()
    event.stopPropagation()
  }
  // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
  return <div id="test-workspace" ref={ref} role="group" aria-label="workspace" tabIndex={-1} onKeyDown={onKeyDown} data-testid="panel" data-fullscreen={fullscreen}>
    <input aria-label="draft" defaultValue="unsaved" />
    <div className="xterm"><textarea aria-label="terminal" onKeyDown={xterm} /></div>
    <input aria-label="find" onKeyDown={event => { if (event.key === 'Escape') event.stopPropagation() }} />
  </div>
}
const noop = () => {}
function Harness({ mounted = noop, terminalSaw }: { mounted?: () => void; terminalSaw?: (defaultPrevented: boolean) => void }) {
  const [fullscreen, setFullscreen] = useState(false)
  return <div data-testid="dashboard-shell">
    <div data-testid="chrome"><button>Chat</button></div>
    <div data-workspace-panel-controls>
      <button onClick={() => setFullscreen(value => !value)}>Toggle</button>
    </div>
    <WorkspaceFullscreenContext.Provider value={{ fullscreen, exit: () => setFullscreen(false), toggle: () => setFullscreen(value => !value) }}>
      <Panel mounted={mounted} terminalSaw={terminalSaw} />
    </WorkspaceFullscreenContext.Provider>
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
    expect(screen.getByTestId('chrome').inert).toBe(true)
    fireEvent.keyDown(draft, { key: 'Escape' })
    expect(panel).toHaveAttribute('data-fullscreen', 'false')
    expect(screen.getByLabelText('draft')).toBe(draft)
    expect(draft).toHaveValue('edited')
    expect(screen.getByTestId('chrome').inert).toBeFalsy()
    expect(mounted).toHaveBeenCalledTimes(1)
  })
  it('yields to nested controls and dialogs, then exits from the panel or shell controls', () => {
    render(<Harness />)
    const toggle = screen.getByText('Toggle')
    fireEvent.click(toggle)
    const panel = screen.getByTestId('panel')
    fireEvent.keyDown(screen.getByLabelText('find'), { key: 'Escape' })
    expect(panel).toHaveAttribute('data-fullscreen', 'true')
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    document.body.append(dialog)
    try {
      fireEvent.keyDown(screen.getByLabelText('draft'), { key: 'Escape' })
      expect(panel).toHaveAttribute('data-fullscreen', 'true')
    } finally { dialog.remove() }
    fireEvent.keyDown(screen.getByLabelText('draft'), { key: 'Escape' })
    expect(panel).toHaveAttribute('data-fullscreen', 'false')
    fireEvent.click(toggle)
    fireEvent.keyDown(toggle, { key: 'Escape' })
    expect(panel).toHaveAttribute('data-fullscreen', 'false')
  })
  // Escape in a terminal is the running program's key (vim's insert mode,
  // less, any TUI). The hook must neither take it before xterm nor exit on it:
  // an intercepted Escape leaves the program in the state the user believes it
  // left, and the next keystrokes edit a buffer they think they are commanding.
  it('leaves Escape to a focused terminal and keeps fullscreen', () => {
    const terminalSaw = vi.fn()
    render(<Harness terminalSaw={terminalSaw} />)
    fireEvent.click(screen.getByText('Toggle'))
    const panel = screen.getByTestId('panel')
    fireEvent.keyDown(screen.getByLabelText('terminal'), { key: 'Escape' })
    expect(terminalSaw).toHaveBeenCalledWith(false)
    expect(panel).toHaveAttribute('data-fullscreen', 'true')
  })
  it('ignores hidden background preview markers but respects the active preview owner', () => {
    render(<Harness />)
    const toggle = screen.getByText('Toggle')
    fireEvent.click(toggle)
    const panel = screen.getByTestId('panel')
    const preview = document.createElement('div')
    const marker = document.createElement('span')
    marker.hidden = true
    marker.setAttribute('data-workspace-escape-owner', '')
    preview.append(marker)
    panel.append(preview)
    try {
      fireEvent.keyDown(screen.getByLabelText('draft'), { key: 'Escape' })
      expect(panel).toHaveAttribute('data-fullscreen', 'true')
      preview.style.display = 'none'
      fireEvent.keyDown(screen.getByLabelText('draft'), { key: 'Escape' })
      expect(panel).toHaveAttribute('data-fullscreen', 'false')
      fireEvent.click(toggle)
      preview.style.display = 'block'
      preview.style.visibility = 'hidden'
      fireEvent.keyDown(screen.getByLabelText('draft'), { key: 'Escape' })
      expect(panel).toHaveAttribute('data-fullscreen', 'false')
    } finally { preview.remove() }
  })

  it('preserves focus deliberately moved to a shell control when exiting', () => {
    render(<Harness />)
    const draft = screen.getByLabelText('draft')
    draft.focus()
    const toggle = screen.getByText('Toggle')
    fireEvent.click(toggle)
    toggle.focus()
    fireEvent.keyDown(toggle, { key: 'Escape' })
    expect(screen.getByTestId('panel')).toHaveAttribute('data-fullscreen', 'false')
    expect(toggle).toHaveFocus()
  })

  it('yields to an owned body portal when focus has returned to the panel', () => {
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
    // Open after fullscreen has made covered chrome inert, matching the real
    // SelectionToolbar portal's lifecycle.
    render(<Annotation />)
    const panel = screen.getByTestId('panel')
    const draft = screen.getByLabelText('draft')
    expect(panel.contains(screen.getByLabelText('annotation draft'))).toBe(false)
    draft.focus()
    fireEvent.keyDown(draft, { key: 'Escape' })
    expect(panel).toHaveAttribute('data-fullscreen', 'true')
    expect(screen.getByLabelText('annotation draft')).toHaveValue('Keep this draft')
    fireEvent.keyUp(draft, { key: 'Escape' })
    expect(screen.queryByLabelText('annotation draft')).not.toBeInTheDocument()
    expect(panel).toHaveAttribute('data-fullscreen', 'true')
    fireEvent.keyDown(draft, { key: 'Escape' })
    expect(panel).toHaveAttribute('data-fullscreen', 'false')
  })

})
