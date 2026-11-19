import { describe, it, expect, vi, afterEach } from 'vitest'
import type { Terminal } from '@xterm/xterm'
import {
  attachOsc52Copy,
  decodeOsc52Copy,
  OSC52_INPUT_WINDOW_MS,
  OSC52_MAX_BYTES,
} from '../lib/terminalOsc52'

/** Base64 of the UTF-8 bytes of `text`, the way a program encodes OSC 52. */
function b64(text: string): string {
  const bytes = new TextEncoder().encode(text)
  return btoa(String.fromCharCode(...bytes))
}

describe('decodeOsc52Copy', () => {
  it('decodes a clipboard copy', () => {
    expect(decodeOsc52Copy(`c;${b64('hello')}`)).toBe('hello')
  })

  it('accepts the empty, primary, select and combined targets', () => {
    for (const target of ['', 'p', 's', 'cp', '0']) {
      expect(decodeOsc52Copy(`${target};${b64('x')}`)).toBe('x')
    }
  })

  it('keeps non-ASCII text intact', () => {
    const text = 'café — 日本語 🚀'
    expect(decodeOsc52Copy(`c;${b64(text)}`)).toBe(text)
  })

  it('keeps a leading U+FEFF', () => {
    expect(decodeOsc52Copy(`c;${b64('\uFEFFa')}`)).toBe('\uFEFFa')
  })

  it('returns null for a read request', () => {
    expect(decodeOsc52Copy('c;?')).toBeNull()
    expect(decodeOsc52Copy(';?')).toBeNull()
  })

  it('returns null for an empty payload, a missing separator or an unknown target', () => {
    expect(decodeOsc52Copy('c;')).toBeNull()
    expect(decodeOsc52Copy('c')).toBeNull()
    expect(decodeOsc52Copy(`x;${b64('hi')}`)).toBeNull()
  })

  it('returns null for malformed base64', () => {
    expect(decodeOsc52Copy('c;not base64!')).toBeNull()
    expect(decodeOsc52Copy('c;abc')).toBeNull()
  })

  it('returns null for bytes that are not UTF-8', () => {
    expect(decodeOsc52Copy(`c;${btoa('\xff\xfe')}`)).toBeNull()
  })

  it('returns null for a payload over the size cap', () => {
    const big = 'a'.repeat(OSC52_MAX_BYTES + 3)
    expect(decodeOsc52Copy(`c;${btoa(big)}`)).toBeNull()
  })

  it('returns null for a payload one byte over the cap that passes the encoded-length check', () => {
    // One byte over still fits the encoded-length bound, so only the check on
    // the decoded length rejects it.
    const over = 'a'.repeat(OSC52_MAX_BYTES + 1)
    const encoded = btoa(over)
    expect(encoded.length).toBeLessThanOrEqual(Math.ceil(OSC52_MAX_BYTES / 3) * 4)
    expect(decodeOsc52Copy(`c;${encoded}`)).toBeNull()
  })

  it('accepts a payload exactly at the size cap', () => {
    const max = 'a'.repeat(OSC52_MAX_BYTES)
    expect(decodeOsc52Copy(`c;${btoa(max)}`)).toBe(max)
  })
})

type MouseMode = 'none' | 'x10' | 'vt200' | 'drag' | 'any'

describe('attachOsc52Copy', () => {
  const cleanups: (() => void)[] = []
  afterEach(() => { while (cleanups.length) cleanups.pop()!() })

  function setup() {
    let osc: ((data: string) => boolean) | undefined
    let onData: ((data: string) => void) | undefined
    let onKey: ((e: { key: string, domEvent: KeyboardEvent }) => void) | undefined
    let disposed = false
    let dataDisposed = false
    let keyDisposed = false
    const element = document.createElement('div')
    const inner = document.createElement('textarea')
    element.appendChild(inner)
    document.body.appendChild(element)
    const modes = { mouseTrackingMode: 'none' as MouseMode }
    const term = {
      element,
      modes,
      onData: (cb: (data: string) => void) => {
        onData = cb
        return { dispose: () => { dataDisposed = true } }
      },
      onKey: (cb: (e: { key: string, domEvent: KeyboardEvent }) => void) => {
        onKey = cb
        return { dispose: () => { keyDisposed = true } }
      },
      parser: {
        registerOscHandler: (id: number, cb: (d: string) => boolean) => {
          expect(id).toBe(52)
          osc = cb
          return { dispose: () => { disposed = true } }
        },
      },
    } as unknown as Terminal
    let clock = 10_000
    const write = vi.fn(async (_text: string) => true)
    const onResult = vi.fn<(ok: boolean) => void>()
    const handle = attachOsc52Copy(term, { now: () => clock, write, onResult })
    cleanups.push(() => { handle.dispose(); element.remove() })

    /** A key xterm sends to the program: the DOM keydown, then onKey, then
     *  onData, in xterm's own order. */
    const press = (data: string, init: KeyboardEventInit = {}) => {
      const domEvent = new KeyboardEvent('keydown', { bubbles: true, ...init })
      inner.dispatchEvent(domEvent)
      onKey!({ key: data, domEvent })
      onData!(data)
    }
    /** A keydown the dashboard's key handler keeps: xterm sends nothing. */
    const keepKey = (init: KeyboardEventInit) => {
      inner.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, ...init }))
    }
    return {
      inner,
      write,
      onResult,
      handle,
      press,
      keepKey,
      setMouseMode: (mode: MouseMode) => { modes.mouseTrackingMode = mode },
      send: (data: string) => osc!(data),
      /** Data with no key behind it: a paste or a terminal reply. */
      type: (data: string) => onData!(data),
      advance: (ms: number) => { clock += ms },
      isDisposed: () => disposed,
      isDataDisposed: () => dataDisposed,
      isKeyDisposed: () => keyDisposed,
    }
  }

  it('copies right after a key press in the terminal', () => {
    const t = setup()
    t.press('y', { key: 'y' })
    expect(t.send(`c;${b64('copied')}`)).toBe(true)
    expect(t.write).toHaveBeenCalledWith('copied')
  })

  it.each(['x10', 'vt200', 'drag', 'any'] as const)(
    'copies right after a pointer release while the program tracks the mouse (%s)',
    (mode) => {
      const t = setup()
      t.setMouseMode(mode)
      t.inner.dispatchEvent(new Event('pointerup', { bubbles: true }))
      t.advance(OSC52_INPUT_WINDOW_MS)
      t.send(`c;${b64('selection')}`)
      expect(t.write).toHaveBeenCalledWith('selection')
    },
  )

  it('copies right after a pointer press while the program tracks the mouse', () => {
    const t = setup()
    t.setMouseMode('vt200')
    t.inner.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    t.send(`c;${b64('selection')}`)
    expect(t.write).toHaveBeenCalledWith('selection')
  })

  it.each(['pointerdown', 'pointerup'])('does not copy after a plain %s with no mouse tracking', (type) => {
    const t = setup()
    t.inner.dispatchEvent(new Event(type, { bubbles: true }))
    t.send(`c;${b64('stream output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('does not copy after a click and drag to select with no mouse tracking', () => {
    const t = setup()
    t.inner.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    t.inner.dispatchEvent(new Event('pointerup', { bubbles: true }))
    t.send(`c;${b64('stream output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it.each([
    ['Shift', { shiftKey: true }],
    ['Alt', { altKey: true }],
  ])('does not copy after a %s+drag selection while the program tracks the mouse', (_name, mods) => {
    // xterm keeps a Shift (or macOS Option) click for its own selection and
    // never sends it to the program.
    const t = setup()
    t.setMouseMode('any')
    t.inner.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, ...mods }))
    t.inner.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, ...mods }))
    t.send(`c;${b64('stream output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('closes the window on closeWindow, after an earlier key press', () => {
    const t = setup()
    t.press('y', { key: 'y' })
    t.handle.closeWindow()
    t.send(`c;${b64('command output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('ignores a pointer release on another element while the program tracks the mouse', () => {
    const t = setup()
    t.setMouseMode('any')
    const other = document.createElement('div')
    document.body.appendChild(other)
    cleanups.push(() => other.remove())
    other.dispatchEvent(new Event('pointerup', { bubbles: true }))
    t.send(`c;${b64('elsewhere')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it.each([
    ['Enter', '\r', {}],
    ['Shift+Enter', '\r', { shiftKey: true }],
    ['Alt+Enter', '\x1b\r', { altKey: true }],
    ['NumpadEnter', '\r', { code: 'NumpadEnter' }],
  ])('does not copy after %s in the terminal', (_name, data, mods) => {
    const t = setup()
    t.press(data, { key: 'Enter', ...mods })
    t.send(`c;${b64('command output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it.each([
    ['a typed command', 't', { key: 't' }],
    ['a recalled command', '\x1b[A', { key: 'ArrowUp' }],
  ])('does not copy output of %s run with Enter', (_name, data, init) => {
    const t = setup()
    t.press(data, init)
    t.press('\r', { key: 'Enter' })
    t.send(`c;${b64('command output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it.each([
    ['Ctrl+C', '\x03', { key: 'c', ctrlKey: true }],
    ['Ctrl+D', '\x04', { key: 'd', ctrlKey: true }],
    ['Ctrl+Z', '\x1a', { key: 'z', ctrlKey: true }],
    ['Ctrl+\\', '\x1c', { key: '\\', ctrlKey: true }],
  ])('does not copy after %s, even right after an earlier key press', (_name, data, init) => {
    const t = setup()
    t.press('y', { key: 'y' })
    t.press(data, init)
    t.send(`c;${b64('stream output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('does not copy after a key the dashboard keeps for a shortcut', () => {
    const t = setup()
    t.keepKey({ key: 'f', ctrlKey: true, shiftKey: true })
    t.send(`c;${b64('stream output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('does not copy after a paste keystroke and its pasted text', () => {
    const t = setup()
    t.keepKey({ key: 'V', ctrlKey: true, shiftKey: true })
    t.type('\x1b[200~echo hi\x1b[201~')
    t.send(`c;${b64('stream output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('does not copy after a reply the terminal sends by itself', () => {
    const t = setup()
    t.type('\x1b[?1;2c')
    t.send(`c;${b64('stream output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('copies again after a key press that follows an Enter', () => {
    const t = setup()
    t.press('\r', { key: 'Enter' })
    t.press('y', { key: 'y' })
    t.send(`c;${b64('yanked')}`)
    expect(t.write).toHaveBeenCalledWith('yanked')
  })

  it.each([
    ['vi-mode y', 'y', { key: 'y' }],
    ['M-w', '\x1bw', { key: 'w', altKey: true }],
  ])('copies right after a %s key press in the terminal', (_name, data, init) => {
    const t = setup()
    t.press(data, init)
    t.send(`c;${b64('yanked')}`)
    expect(t.write).toHaveBeenCalledWith('yanked')
  })

  it('does not answer a read request', () => {
    const t = setup()
    t.press('y', { key: 'y' })
    expect(t.send('c;?')).toBe(true)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('leaves the clipboard alone with no input on this terminal', () => {
    const t = setup()
    t.send(`c;${b64('unrequested')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('leaves the clipboard alone once the input window has passed', () => {
    const t = setup()
    t.press('y', { key: 'y' })
    t.advance(OSC52_INPUT_WINDOW_MS + 1)
    t.send(`c;${b64('late')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('ignores a keydown on another element', () => {
    const t = setup()
    const other = document.createElement('input')
    document.body.appendChild(other)
    cleanups.push(() => other.remove())
    other.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true }))
    t.send(`c;${b64('elsewhere')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it.each([false, true])('reports onResult(%s) when the clipboard write resolves', async (ok) => {
    const t = setup()
    t.write.mockResolvedValue(ok)
    t.press('y', { key: 'y' })
    expect(t.send(`c;${b64('copied')}`)).toBe(true)
    await vi.waitFor(() => expect(t.onResult).toHaveBeenCalledExactlyOnceWith(ok))
  })

  it('reports onResult(false) when the clipboard write rejects', async () => {
    const t = setup()
    t.write.mockRejectedValue(new Error('copy unavailable'))
    t.press('y', { key: 'y' })
    expect(t.send(`c;${b64('copied')}`)).toBe(true)
    await vi.waitFor(() => expect(t.onResult).toHaveBeenCalledExactlyOnceWith(false))
  })

  it('does not report results for ignored requests', async () => {
    const t = setup()
    t.send(`c;${b64('no input')}`)
    t.press('y', { key: 'y' })
    t.send('c;?')
    t.send('c;not base64!')
    t.advance(OSC52_INPUT_WINDOW_MS + 1)
    t.send(`c;${b64('late')}`)
    await Promise.resolve()
    expect(t.write).not.toHaveBeenCalled()
    expect(t.onResult).not.toHaveBeenCalled()
  })

  it.each([
    ['Ctrl+M', '\r', { key: 'm', ctrlKey: true }],
    ['Ctrl+J', '\n', { key: 'j', ctrlKey: true }],
  ])('does not copy output of a command submitted with %s', (_name, data, init) => {
    const t = setup()
    t.press(data, init)
    t.send(`c;${b64('command output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('closes the window on a pasted line that ends in a newline', () => {
    const t = setup()
    t.press('y', { key: 'y' })
    t.type('\x1b[200~cat notes.txt\r\x1b[201~')
    t.send(`c;${b64('command output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('still copies after a key whose data submits nothing', () => {
    const t = setup()
    t.press('y', { key: 'y' })
    t.type('y')
    t.send(`c;${b64('yanked')}`)
    expect(t.write).toHaveBeenCalledWith('yanked')
  })

  it('ignores a copy after suspend, even right after an earlier key press', () => {
    const t = setup()
    t.press('y', { key: 'y' })
    t.handle.suspend()
    expect(t.send(`c;${b64('replayed')}`)).toBe(true)
    expect(t.write).not.toHaveBeenCalled()
  })

  it.each([
    ['a key press', (t: ReturnType<typeof setup>) => t.press('y', { key: 'y' })],
    ['a pointer release', (t: ReturnType<typeof setup>) => t.inner.dispatchEvent(new Event('pointerup', { bubbles: true }))],
    ['a pointer press', (t: ReturnType<typeof setup>) => t.inner.dispatchEvent(new Event('pointerdown', { bubbles: true }))],
  ])('ignores a copy after %s made while suspended', (_name, act) => {
    const t = setup()
    t.setMouseMode('any')
    t.handle.suspend()
    act(t)
    t.send(`c;${b64('replayed')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('needs a new key press after resume before a copy applies', () => {
    const t = setup()
    t.press('y', { key: 'y' })
    t.handle.suspend()
    t.press('y', { key: 'y' })
    t.handle.resume()
    t.send(`c;${b64('replayed')}`)
    expect(t.write).not.toHaveBeenCalled()
    t.press('y', { key: 'y' })
    t.send(`c;${b64('yanked')}`)
    expect(t.write).toHaveBeenCalledExactlyOnceWith('yanked')
  })

  it('treats repeated suspend and resume calls as one', () => {
    const t = setup()
    t.handle.suspend()
    t.handle.suspend()
    t.handle.resume()
    t.handle.resume()
    t.press('y', { key: 'y' })
    t.send(`c;${b64('yanked')}`)
    expect(t.write).toHaveBeenCalledExactlyOnceWith('yanked')
  })

  it('removes its handler and listeners on dispose', () => {
    const t = setup()
    t.setMouseMode('any')
    t.handle.dispose()
    expect(t.isDisposed()).toBe(true)
    expect(t.isDataDisposed()).toBe(true)
    expect(t.isKeyDisposed()).toBe(true)
    t.inner.dispatchEvent(new Event('pointerup', { bubbles: true }))
    // The parser handler is gone with dispose, so nothing is left to copy;
    // this only checks the pointer listener throws nothing once removed.
    expect(t.write).not.toHaveBeenCalled()
  })
})
