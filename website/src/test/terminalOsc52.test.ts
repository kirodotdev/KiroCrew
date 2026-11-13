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

  it('accepts a payload exactly at the size cap', () => {
    const max = 'a'.repeat(OSC52_MAX_BYTES)
    expect(decodeOsc52Copy(`c;${btoa(max)}`)).toBe(max)
  })
})

describe('attachOsc52Copy', () => {
  const cleanups: (() => void)[] = []
  afterEach(() => { while (cleanups.length) cleanups.pop()!() })

  function setup() {
    let osc: ((data: string) => boolean) | undefined
    let onData: ((data: string) => void) | undefined
    let disposed = false
    let dataDisposed = false
    const element = document.createElement('div')
    const inner = document.createElement('textarea')
    element.appendChild(inner)
    document.body.appendChild(element)
    const term = {
      element,
      onData: (cb: (data: string) => void) => {
        onData = cb
        return { dispose: () => { dataDisposed = true } }
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
    return {
      inner,
      write,
      onResult,
      handle,
      send: (data: string) => osc!(data),
      type: (data: string) => onData!(data),
      advance: (ms: number) => { clock += ms },
      isDisposed: () => disposed,
      isDataDisposed: () => dataDisposed,
    }
  }

  it('copies right after a key press in the terminal', () => {
    const t = setup()
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true }))
    expect(t.send(`c;${b64('copied')}`)).toBe(true)
    expect(t.write).toHaveBeenCalledWith('copied')
  })

  it('copies right after a pointer release in the terminal', () => {
    const t = setup()
    t.inner.dispatchEvent(new Event('pointerup', { bubbles: true }))
    t.advance(OSC52_INPUT_WINDOW_MS)
    t.send(`c;${b64('selection')}`)
    expect(t.write).toHaveBeenCalledWith('selection')
  })

  it.each([
    {},
    { shiftKey: true },
    { ctrlKey: true },
    { altKey: true },
    { metaKey: true },
    { code: 'NumpadEnter' },
  ])('does not copy after an Enter press in the terminal (%o)', (mods) => {
    const t = setup()
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, ...mods }))
    t.send(`c;${b64('command output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it.each([
    ['a typed command', { key: 't' }],
    ['a recalled command', { key: 'ArrowUp' }],
  ])('does not copy output of %s run with Enter', (_name, init) => {
    const t = setup()
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, ...init }))
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    t.send(`c;${b64('command output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('copies again after a key press that follows an Enter', () => {
    const t = setup()
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { key: 'y', bubbles: true }))
    t.send(`c;${b64('yanked')}`)
    expect(t.write).toHaveBeenCalledWith('yanked')
  })

  it('ignores an Enter press on another element', () => {
    const t = setup()
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { key: 'y', bubbles: true }))
    const other = document.createElement('input')
    document.body.appendChild(other)
    cleanups.push(() => other.remove())
    other.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    t.send(`c;${b64('still mine')}`)
    expect(t.write).toHaveBeenCalledWith('still mine')
  })

  it.each([
    ['vi-mode y', { key: 'y' }],
    ['M-w', { key: 'w', altKey: true }],
  ])('copies right after a %s key press in the terminal', (_name, init) => {
    const t = setup()
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, ...init }))
    t.send(`c;${b64('yanked')}`)
    expect(t.write).toHaveBeenCalledWith('yanked')
  })

  it('does not answer a read request', () => {
    const t = setup()
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true }))
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
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true }))
    t.advance(OSC52_INPUT_WINDOW_MS + 1)
    t.send(`c;${b64('late')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('ignores input on another element', () => {
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
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true }))
    expect(t.send(`c;${b64('copied')}`)).toBe(true)
    await vi.waitFor(() => expect(t.onResult).toHaveBeenCalledExactlyOnceWith(ok))
  })

  it('reports onResult(false) when the clipboard write rejects', async () => {
    const t = setup()
    t.write.mockRejectedValue(new Error('copy unavailable'))
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true }))
    expect(t.send(`c;${b64('copied')}`)).toBe(true)
    await vi.waitFor(() => expect(t.onResult).toHaveBeenCalledExactlyOnceWith(false))
  })

  it('does not report results for ignored requests', async () => {
    const t = setup()
    t.send(`c;${b64('no input')}`)
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true }))
    t.send('c;?')
    t.send('c;not base64!')
    t.advance(OSC52_INPUT_WINDOW_MS + 1)
    t.send(`c;${b64('late')}`)
    await Promise.resolve()
    expect(t.write).not.toHaveBeenCalled()
    expect(t.onResult).not.toHaveBeenCalled()
  })

  it.each([
    ['Ctrl+M', { key: 'm', ctrlKey: true }, '\r'],
    ['Ctrl+J', { key: 'j', ctrlKey: true }, '\n'],
  ])('does not copy output of a command submitted with %s', (_name, init, data) => {
    const t = setup()
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, ...init }))
    t.type(data)
    t.send(`c;${b64('command output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('does not copy output of a pasted line that ends in a newline', () => {
    const t = setup()
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { key: 'v', ctrlKey: true, shiftKey: true, bubbles: true }))
    t.type('\x1b[200~cat notes.txt\r\x1b[201~')
    t.send(`c;${b64('command output')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('still copies after a key whose data submits nothing', () => {
    const t = setup()
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { key: 'y', bubbles: true }))
    t.type('y')
    t.send(`c;${b64('yanked')}`)
    expect(t.write).toHaveBeenCalledWith('yanked')
  })

  it('ignores a copy after suspend, even right after an earlier key press', () => {
    const t = setup()
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { key: 'y', bubbles: true }))
    t.handle.suspend()
    expect(t.send(`c;${b64('replayed')}`)).toBe(true)
    expect(t.write).not.toHaveBeenCalled()
  })

  it.each([
    ['a key press', () => new KeyboardEvent('keydown', { key: 'y', bubbles: true })],
    ['a pointer release', () => new Event('pointerup', { bubbles: true })],
    ['a pointer press', () => new Event('pointerdown', { bubbles: true })],
  ])('ignores a copy after %s made while suspended', (_name, make) => {
    const t = setup()
    t.handle.suspend()
    t.inner.dispatchEvent(make())
    t.send(`c;${b64('replayed')}`)
    expect(t.write).not.toHaveBeenCalled()
  })

  it('needs a new key press after resume before a copy applies', () => {
    const t = setup()
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { key: 'y', bubbles: true }))
    t.handle.suspend()
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { key: 'y', bubbles: true }))
    t.handle.resume()
    t.send(`c;${b64('replayed')}`)
    expect(t.write).not.toHaveBeenCalled()
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { key: 'y', bubbles: true }))
    t.send(`c;${b64('yanked')}`)
    expect(t.write).toHaveBeenCalledExactlyOnceWith('yanked')
  })

  it('treats repeated suspend and resume calls as one', () => {
    const t = setup()
    t.handle.suspend()
    t.handle.suspend()
    t.handle.resume()
    t.handle.resume()
    t.inner.dispatchEvent(new KeyboardEvent('keydown', { key: 'y', bubbles: true }))
    t.send(`c;${b64('yanked')}`)
    expect(t.write).toHaveBeenCalledExactlyOnceWith('yanked')
  })

  it('removes its handler and listeners on dispose', () => {
    const t = setup()
    t.handle.dispose()
    expect(t.isDisposed()).toBe(true)
    expect(t.isDataDisposed()).toBe(true)
  })
})
