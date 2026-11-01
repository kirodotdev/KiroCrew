/**
 * A terminal built ahead of time (prewarmTerminal) can sit in the cache before
 * any CliPanel mounts, so it must follow terminal-font changes from the moment
 * it is built. Own file: the font subscription is module-level and starts
 * once, so a fresh module is the only place it can be observed starting.
 */
import { describe, it, expect, vi } from 'vitest'
import { act } from '@testing-library/react'

const xt = vi.hoisted(() => {
  class FakeTerminal {
    static instances: FakeTerminal[] = []
    options: Record<string, unknown>
    element: HTMLElement | undefined = undefined
    constructor(options: Record<string, unknown>) { this.options = { ...options }; FakeTerminal.instances.push(this) }
    loadAddon() {}
    open(container: HTMLElement) { const el = document.createElement('div'); container.appendChild(el); this.element = el }
    dispose() {}
  }
  class FakeFitAddon { fit = vi.fn() }
  class FakeWebLinksAddon {}
  return { FakeTerminal, FakeFitAddon, FakeWebLinksAddon }
})
vi.mock('@xterm/xterm', () => ({ Terminal: xt.FakeTerminal }))
vi.mock('@xterm/addon-fit', () => ({ FitAddon: xt.FakeFitAddon }))
vi.mock('@xterm/addon-web-links', () => ({ WebLinksAddon: xt.FakeWebLinksAddon }))

import { prewarmTerminal, disposeTerminalSession } from '../components/CliPanel'
import { setTerminalFontSize, __resetTerminalFontStore } from '../hooks/useTerminalFont'

describe('prewarmTerminal', () => {
  it('follows a terminal font change made before any pane mounts', async () => {
    try {
      prewarmTerminal('pty-prewarm-sync', 390, 300)
      const term = xt.FakeTerminal.instances[xt.FakeTerminal.instances.length - 1]
      act(() => { setTerminalFontSize(19) })
      await act(async () => { await new Promise(r => requestAnimationFrame(() => r(undefined))) })
      expect(term.options.fontSize).toBe(19)
    } finally {
      disposeTerminalSession('pty-prewarm-sync')
      __resetTerminalFontStore()
    }
  })
})
