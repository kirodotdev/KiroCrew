/**
 * Where the keyboard lands after the chord closes the docked terminal.
 *
 * The skip-shell listener exists so the chord fires while xterm's textarea has
 * focus — which means the close unmounts the subtree focus was inside, dropping
 * focus onto `<body>`. In a shortcut whose purpose is keeping hands off the mouse,
 * that is the worst possible landing. These tests pin the recovery: back to
 * wherever the chord was pressed from, or to the app's `#main-content` landing
 * zone when that element is gone.
 *
 * The nav rail's button path is deliberately NOT covered here, because it does not
 * have the problem — focus sits on the button, outside the panel, and survives.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

import { newTerminalByChord, toggleTerminalByChord, __resetTerminalChordFocus } from '../lib/terminalChordFocus'
import { isBottomTerminalOpen, useBottomTerminal, __resetBottomTerminal, MAX_TERMINALS } from '../hooks/useBottomTerminal'
import { renderHook } from '@testing-library/react'

/** A stand-in for the docked panel: an `.xterm` subtree with a focusable node,
 *  removed on close the way React's `{open && …}` removes the real one. */
function mountFakeTerminal(): { shellInput: HTMLTextAreaElement; unmount: () => void } {
  const term = document.createElement('div')
  term.className = 'xterm'
  const shellInput = document.createElement('textarea')
  term.appendChild(shellInput)
  document.body.appendChild(term)
  return { shellInput, unmount: () => term.remove() }
}

function mountMainContent(): HTMLElement {
  const main = document.createElement('main')
  main.id = 'main-content'
  main.tabIndex = -1
  document.body.appendChild(main)
  return main
}

describe('toggleTerminalByChord: focus after a close-from-shell', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetBottomTerminal()
    __resetTerminalChordFocus()
    // `replaceChildren()` rather than an HTML-string assignment: the repo's
    // Automated Rule Check blocks that property outright, tests included.
    document.body.replaceChildren()
    // Run the restore synchronously so the assertions do not need to await a frame.
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 0 })
  })
  afterEach(() => { vi.unstubAllGlobals(); document.body.replaceChildren() })

  it('puts focus back where the chord was pressed from', () => {
    const composer = document.createElement('textarea')
    document.body.appendChild(composer)
    composer.focus()
    expect(document.activeElement).toBe(composer)

    toggleTerminalByChord()                       // open, from the composer
    expect(isBottomTerminalOpen()).toBe(true)
    const term = mountFakeTerminal()
    term.shellInput.focus()                       // the panel takes focus, as it does live

    toggleTerminalByChord()                       // close, from inside the shell
    term.unmount()
    expect(isBottomTerminalOpen()).toBe(false)
    expect(document.activeElement).toBe(composer)
  })

  it('falls back to #main-content when the remembered element is gone', () => {
    const main = mountMainContent()
    const gone = document.createElement('button')
    document.body.appendChild(gone)
    gone.focus()

    toggleTerminalByChord()
    const term = mountFakeTerminal()
    term.shellInput.focus()
    gone.remove()                                 // navigated away while the panel was open

    toggleTerminalByChord()
    term.unmount()
    expect(document.activeElement).toBe(main)
  })

  it('leaves focus alone when the close came from outside the terminal', () => {
    const composer = document.createElement('textarea')
    const elsewhere = document.createElement('input')
    document.body.append(composer, elsewhere)
    composer.focus()

    toggleTerminalByChord()
    mountFakeTerminal()
    elsewhere.focus()                             // user clicked back out of the shell

    toggleTerminalByChord()
    // Their own choice of focus wins over anything this module remembered.
    expect(document.activeElement).toBe(elsewhere)
  })

  it('still toggles the panel, which is the part the chord actually promises', () => {
    toggleTerminalByChord()
    expect(isBottomTerminalOpen()).toBe(true)
    toggleTerminalByChord()
    expect(isBottomTerminalOpen()).toBe(false)
  })
})

describe('newTerminalByChord: VS Code\'s Create New Terminal', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetBottomTerminal()
    __resetTerminalChordFocus()
    document.body.replaceChildren()
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 0 })
  })
  afterEach(() => { vi.unstubAllGlobals(); document.body.replaceChildren() })

  const tabCount = () => renderHook(() => useBottomTerminal()).result.current.tabs.length

  it('opens the panel and adds a tab on every press, never closing it', () => {
    newTerminalByChord('/proj')
    expect(isBottomTerminalOpen()).toBe(true)
    expect(tabCount()).toBe(1)
    newTerminalByChord('/proj')
    expect(isBottomTerminalOpen()).toBe(true)
    expect(tabCount()).toBe(2)
  })

  it('stops adding at the terminal cap but keeps the panel open', () => {
    for (let i = 0; i < MAX_TERMINALS + 2; i++) newTerminalByChord()
    expect(isBottomTerminalOpen()).toBe(true)
    expect(tabCount()).toBe(MAX_TERMINALS)
  })

  it('remembers where it opened from, so the toggle chord closing from a shell returns there', () => {
    const composer = document.createElement('textarea')
    document.body.appendChild(composer)
    composer.focus()

    newTerminalByChord()                          // open + add, from the composer
    const term = mountFakeTerminal()
    term.shellInput.focus()

    toggleTerminalByChord()                       // close, from inside the shell
    term.unmount()
    expect(isBottomTerminalOpen()).toBe(false)
    expect(document.activeElement).toBe(composer)
  })
})
