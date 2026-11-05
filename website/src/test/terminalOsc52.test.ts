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
    let disposed = false
    const element = document.createElement('div')
    const inner = document.createElement('textarea')
    element.appendChild(inner)
    document.body.appendChild(element)
    const term = {
      element,
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
    const handle = attachOsc52Copy(term, { now: () => clock, write })
    cleanups.push(() => { handle.dispose(); element.remove() })
    return {
      inner,
      write,
      handle,
      send: (data: string) => osc!(data),
      advance: (ms: number) => { clock += ms },
      isDisposed: () => disposed,
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

  it('removes its handler and listeners on dispose', () => {
    const t = setup()
    t.handle.dispose()
    expect(t.isDisposed()).toBe(true)
  })
})
