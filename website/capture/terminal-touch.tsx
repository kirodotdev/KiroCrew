/**
 * Isolated capture entry for phone touch in the terminal: momentum scrolling,
 * long-press character selection with drag handles, and the key bar's Copy.
 *
 * It mounts a REAL xterm with the same pieces CliPanel wires on a touch device:
 * the useTerminalTouchSelection hook bound on the grid wrapper, the
 * TerminalTouchHandles layer inside it, and TerminalKeyBar below. No socket:
 * the scrollback is written from a fixture, so the page needs no gateway.
 * scripts/capture-terminal-touch.mjs drives it with emulated phone touch.
 *
 * Theme comes from the query string: ?theme=light
 */
import { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Terminal } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'

import { initI18n } from '../src/i18n/all'
import { useIsTouchDevice } from '../src/hooks/useIsTouchDevice'
import { useTerminalTouchSelection } from '../src/hooks/useTerminalTouchSelection'
import TerminalTouchHandles from '../src/components/TerminalTouchHandles'
import TerminalKeyBar from '../src/components/TerminalKeyBar'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** Mocked shell output: enough scrollback to fling through. */
function fixture(): string {
  const out: string[] = ['$ git log --oneline -200']
  for (let i = 200; i > 0; i--) {
    const sha = (0x9f3c2a1 + i * 7919).toString(16).slice(0, 7)
    out.push(`${sha} fix: commit ${i} of the log`)
  }
  out.push('$ ')
  return out.join('\r\n')
}

const term = new Terminal({ fontSize: 13, cols: 40, rows: 24, scrollback: 1000 })
;(window as unknown as { __term: Terminal }).__term = term

function Harness() {
  const gridRef = useRef<HTMLDivElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const touchDevice = useIsTouchDevice()
  const [ready, setReady] = useState(false)
  useEffect(() => {
    if (!containerRef.current || term.element) return
    term.open(containerRef.current)
    term.write(fixture(), () => setReady(true))
  }, [])
  const touchSelect = useTerminalTouchSelection(term, touchDevice, gridRef)
  return (
    <div className="flex h-screen flex-col px-3 pt-2" style={{ background: 'var(--term-bg, var(--bg))' }} data-ready={ready ? '1' : '0'}>
      <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
        <div ref={gridRef} className="relative min-h-0 w-full flex-1 overflow-hidden" data-testid="terminal-grid">
          <div ref={containerRef} className="h-full w-full overflow-hidden" />
          {touchDevice && <TerminalTouchHandles handles={touchSelect.handles} />}
        </div>
        {touchDevice && <TerminalKeyBar term={term} />}
      </div>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Harness />)
