/**
 * Keys typed while the terminal loads reach the shell as typed: in order, raw,
 * with their Enter, once the socket opens (#43268). Real LazyCliPanel, real
 * CliPanel, real terminalRegistry; only xterm and the socket are fakes.
 *
 * The keys cross three places on the way: the stand-in shown while the chunk
 * loads, CliPanel's own stand-in while the dock holds xterm, and xterm before
 * the socket is open. Before the fix the first two each kept their own text,
 * the hand-off rewrote it as one paste with its Enter removed, and xterm's keys
 * were dropped.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'

const xt = vi.hoisted(() => {
  class FakeTerminal {
    static instances: FakeTerminal[] = []
    options: Record<string, unknown>
    element: HTMLElement | undefined = undefined
    textarea: HTMLTextAreaElement | undefined = undefined
    cols = 80
    rows = 24
    modes = { mouseTrackingMode: 'none', bracketedPasteMode: false }
    parser = { registerOscHandler: () => ({ dispose() {} }) }
    dataCbs: ((data: string) => void)[] = []
    constructor(options: Record<string, unknown>) { this.options = { ...options }; FakeTerminal.instances.push(this) }
    loadAddon() {}
    open(container: HTMLElement) {
      const el = document.createElement('div')
      const ta = document.createElement('textarea')
      el.appendChild(ta)
      container.appendChild(el)
      this.element = el
      this.textarea = ta
    }
    /** What xterm emits for a key typed into it. */
    emit(data: string) { for (const cb of this.dataCbs) cb(data) }
    onData(cb: (data: string) => void) { this.dataCbs.push(cb); return { dispose() {} } }
    onResize() { return { dispose() {} } }
    onKey() { return { dispose() {} } }
    onScroll() { return { dispose() {} } }
    onSelectionChange() { return { dispose() {} } }
    hasSelection() { return false }
    getSelection() { return '' }
    clearSelection() {}
    attachCustomKeyEventHandler() {}
    // As xterm: newlines become CR, bracketed only once the shell asked for it.
    paste(data: string) {
      const text = data.replace(/\r?\n/g, '\r')
      this.emit(this.modes.bracketedPasteMode ? `\x1b[200~${text}\x1b[201~` : text)
    }
    focus() { this.textarea?.focus() }
    reset() {}
    writes: string[] = []
    write(data: string | Uint8Array) { if (typeof data === 'string') this.writes.push(data) }
    refresh() {}
    scrollToBottom() {}
    dispose() {}
  }
  class FakeFitAddon { fit() {} proposeDimensions() { return { cols: 80, rows: 24 } } }
  class FakeWebLinksAddon {}
  class FakeSearchAddon { findNext() {} findPrevious() {} clearDecorations() {} onDidChangeResults() { return { dispose() {} } } }

  class FakeSocket {
    static CONNECTING = 0
    static OPEN = 1
    static CLOSING = 2
    static CLOSED = 3
    static instances: FakeSocket[] = []
    readyState = FakeSocket.CONNECTING
    binaryType = 'blob'
    sent: string[] = []
    onopen: (() => void) | null = null
    onmessage: ((ev: MessageEvent) => void) | null = null
    onclose: (() => void) | null = null
    onerror: (() => void) | null = null
    constructor(public url: string) { FakeSocket.instances.push(this) }
    send(data: string | Uint8Array) { this.sent.push(typeof data === 'string' ? data : new TextDecoder().decode(data)) }
    close() { this.readyState = FakeSocket.CLOSED }
    open() { this.readyState = FakeSocket.OPEN; this.onopen?.() }
    /** Everything sent as keys: frames that are not JSON control messages. */
    typed() { return this.sent.filter(s => !s.startsWith('{')).join('') }
    /** The server's frame that the dial closed (or failed) before or after opening. */
    fail() { this.readyState = FakeSocket.CLOSED; (this.onclose as ((ev: { code: number }) => void) | null)?.({ code: 1006 }) }
  }
  return { FakeTerminal, FakeFitAddon, FakeWebLinksAddon, FakeSearchAddon, FakeSocket }
})
vi.mock('@xterm/xterm', () => ({ Terminal: xt.FakeTerminal }))
vi.mock('@xterm/addon-fit', () => ({ FitAddon: xt.FakeFitAddon }))
vi.mock('@xterm/addon-web-links', () => ({ WebLinksAddon: xt.FakeWebLinksAddon }))
vi.mock('@xterm/addon-search', () => ({ SearchAddon: xt.FakeSearchAddon }))
vi.mock('../components/TerminalCompletion', () => ({ default: () => null }))
vi.mock('../components/TerminalKeyBar', () => ({ default: () => null }))

import LazyCliPanel from '../components/LazyCliPanel'
import { __forgetTerminalModuleForTests, disposeTerminalSession } from '../components/terminalSessions'
import { EARLY_INPUT_CAP } from '../utils/terminalRegistry'

/** Bound on each wait for the lazy `import('./CliPanel')`: the 1000 ms default races a loaded shard. */
const LAZY_CHUNK_TIMEOUT_MS = 5000

// Not shuffled: LazyCliPanel's lazy boundary stays resolved for the module's
// life, so only the first test in this file sees the loading stand-in.
describe('keys typed while the terminal loads', { shuffle: false }, () => {
  const ids: string[] = []
  beforeEach(() => {
    vi.stubGlobal('WebSocket', xt.FakeSocket)
    __forgetTerminalModuleForTests()
    xt.FakeSocket.instances.length = 0
  })
  afterEach(() => {
    for (const id of ids.splice(0)) disposeTerminalSession(id)
    vi.unstubAllGlobals()
  })

  it('reach the shell in typed order, as xterm would send them, with their Enter, once the socket opens', async () => {
    const user = userEvent.setup()
    const sessionId = 'keys-during-load'
    ids.push(sessionId)
    // The dock holds xterm while it slides open (deferOpen), so the keys cross
    // both stand-ins, then xterm, before the socket opens.
    const view = renderWithProviders(<LazyCliPanel sessionId={sessionId} visible deferOpen />)

    // 1. The stand-in shown while the chunk loads.
    expect(document.activeElement).toBe(screen.getByTestId('terminal-loading-focus-proxy'))
    await user.keyboard('ec')

    // 2. CliPanel's own stand-in while the dock holds xterm. Waits on the lazy import.
    const hold = await screen.findByTestId('terminal-focus-proxy', undefined, { timeout: LAZY_CHUNK_TIMEOUT_MS })
    expect(document.activeElement).toBe(hold)
    // Editing keys too: the shell's line editor applies them (echo → ecXho).
    await user.keyboard('ho{ArrowLeft}{ArrowLeft}X{Control>}e{/Control}{Enter}pw')

    // 3. xterm, opened, before its socket is open.
    view.rerender(<LazyCliPanel sessionId={sessionId} visible deferOpen={false} />)
    const term = xt.FakeTerminal.instances[xt.FakeTerminal.instances.length - 1]
    await waitFor(() => expect(term.element).toBeDefined())
    term.emit('d')

    const ws = xt.FakeSocket.instances[xt.FakeSocket.instances.length - 1]
    expect(ws.typed()).toBe('')
    ws.open()
    expect(ws.typed()).toBe('echo\x1b[D\x1b[DX\x05\rpwd')

    // From now on keys go straight to the open socket, after the held ones.
    term.emit('\r')
    expect(ws.typed()).toBe('echo\x1b[D\x1b[DX\x05\rpwd\r')
  })

  it('hold a paste until the shell turns bracketed paste on, with keys typed after it queued behind', async () => {
    const user = userEvent.setup()
    const sessionId = 'paste-during-load'
    ids.push(sessionId)
    const view = renderWithProviders(<LazyCliPanel sessionId={sessionId} visible deferOpen />)
    const hold = await screen.findByTestId('terminal-focus-proxy', undefined, { timeout: LAZY_CHUNK_TIMEOUT_MS })
    hold.focus()
    await user.paste('echo one\necho two')
    await user.keyboard('X')
    view.rerender(<LazyCliPanel sessionId={sessionId} visible deferOpen={false} />)
    const term = xt.FakeTerminal.instances[xt.FakeTerminal.instances.length - 1]
    const ws = xt.FakeSocket.instances[xt.FakeSocket.instances.length - 1]
    ws.open()
    // The shell has not printed its prompt yet: a raw newline would run line 1.
    expect(ws.typed()).toBe('')
    term.emit('Y')
    term.modes.bracketedPasteMode = true // the prompt's \e[?2004h
    // Waits on the held paste's poll (50 ms).
    await waitFor(() => expect(ws.typed()).toBe('\x1b[200~echo one\recho two\x1b[201~XY'), { timeout: 2000 })
  })

  it('hold a paste into xterm itself before its socket opens, and a soft keyboard Backspace', async () => {
    const sessionId = 'xterm-paste-before-open'
    ids.push(sessionId)
    const view = renderWithProviders(<LazyCliPanel sessionId={sessionId} visible deferOpen />)
    const field = screen.getByTestId('terminal-focus-proxy')
    fireEvent.input(field, { target: { value: ' ab' } })
    // A soft keyboard sends no key code: it deletes the field's sentinel, and
    // its Enter can come as a bare line break, which goes as Enter's CR.
    fireEvent.input(field, { target: { value: '' }, inputType: 'deleteContentBackward' })
    fireEvent.input(field, { target: { value: ' \n' }, inputType: 'insertLineBreak' })
    view.rerender(<LazyCliPanel sessionId={sessionId} visible deferOpen={false} />)
    const term = xt.FakeTerminal.instances[xt.FakeTerminal.instances.length - 1]
    await waitFor(() => expect(term.textarea).toBeDefined())
    const paste = new Event('paste', { bubbles: true, cancelable: true })
    Object.defineProperty(paste, 'clipboardData', { value: { getData: () => 'echo p\necho q' } })
    term.textarea!.dispatchEvent(paste)
    expect(paste.defaultPrevented).toBe(true)
    const ws = xt.FakeSocket.instances[xt.FakeSocket.instances.length - 1]
    ws.open()
    expect(ws.typed()).toBe('ab\x7f\r')
    term.modes.bracketedPasteMode = true
    // Waits on the held paste's poll (50 ms).
    await waitFor(() => expect(ws.typed()).toBe('ab\x7f\r\x1b[200~echo p\recho q\x1b[201~'), { timeout: 2000 })
  })

  it('keep at most EARLY_INPUT_CAP UTF-8 bytes typed before the socket opens, cut between code points, and say so', async () => {
    const sessionId = 'keys-cap'
    ids.push(sessionId)
    renderWithProviders(<LazyCliPanel sessionId={sessionId} visible deferOpen />)
    const field = screen.getByTestId('terminal-focus-proxy')
    // 4 UTF-8 bytes (2 UTF-16 units) each, after 3 ASCII bytes: the cap falls inside one.
    fireEvent.input(field, { target: { value: ' abc' + '\u{1F600}'.repeat(20000) } })
    const ws = xt.FakeSocket.instances[xt.FakeSocket.instances.length - 1]
    ws.open()
    const sent = ws.typed()
    expect(new TextEncoder().encode(sent).length).toBe(3 + Math.floor((EARLY_INPUT_CAP - 3) / 4) * 4)
    expect(sent).not.toContain('\uFFFD')
    expect([...sent.slice(3)].every(ch => ch === '\u{1F600}')).toBe(true)
    // The pane says that input was dropped, until it is dismissed.
    expect(await screen.findByTestId('cli-panel-early-input-capped')).toHaveTextContent('64 KB')
  })

  it('drop what was typed when the first dial fails, instead of replaying it on a later one', async () => {
    const sessionId = 'keys-failed-dial'
    ids.push(sessionId)
    renderWithProviders(<LazyCliPanel sessionId={sessionId} visible deferOpen />)
    const field = screen.getByTestId('terminal-focus-proxy')
    fireEvent.input(field, { target: { value: 'rm x' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    const first = xt.FakeSocket.instances[xt.FakeSocket.instances.length - 1]
    first.fail()
    // The redial comes after a backoff; open whichever socket exists by then.
    await waitFor(() => expect(xt.FakeSocket.instances.length).toBeGreaterThan(1), { timeout: 5000 })
    const second = xt.FakeSocket.instances[xt.FakeSocket.instances.length - 1]
    second.open()
    expect(first.typed() + second.typed()).toBe('')
  })

  it('send nothing for a tab closed before its socket opened', async () => {
    const user = userEvent.setup()
    const sessionId = 'keys-closed-before-open'
    ids.push(sessionId)
    renderWithProviders(<LazyCliPanel sessionId={sessionId} visible />)
    await user.keyboard('ls{Enter}')
    await waitFor(() => expect(xt.FakeSocket.instances.length).toBe(1), { timeout: LAZY_CHUNK_TIMEOUT_MS })
    const ws = xt.FakeSocket.instances[0]
    disposeTerminalSession(sessionId)
    ws.open()
    expect(ws.typed()).toBe('')
  })
})
