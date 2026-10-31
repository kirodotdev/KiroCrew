import { describe, it, expect } from 'vitest'

import {
  drawnRect, keyToEvent, mapPointToFrame, modifiersOf, pointerToEvent, wheelToEvent, isHandBackKey,
  diffToEvents, focusOfReply, isSwipe, swipeToWheel, TAP_SLOP_PX, keyboardHints, proxyKeyIsForwarded, textToEvents, TEXT_CHUNK,
  MOD_ALT, MOD_CTRL, MOD_META, MOD_SHIFT, nextClickCount, isImePlaceholderKey, MULTI_CLICK_MS, MULTI_CLICK_PX,
  desktopProxyKeyIsForwarded, keyUpFor,
} from '../hooks/useBrowserLive.input'

const noMods = { altKey: false, ctrlKey: false, metaKey: false, shiftKey: false }
const frame = { w: 1280, h: 800 }

describe('mapPointToFrame', () => {
  it('scales a point in a box that matches the frame aspect', () => {
    const box = { left: 100, top: 50, width: 640, height: 400 }
    expect(mapPointToFrame(100, 50, box, frame)).toEqual({ x: 0, y: 0 })
    expect(mapPointToFrame(420, 250, box, frame)).toEqual({ x: 640, y: 400 })
    expect(mapPointToFrame(740, 450, box, frame)).toEqual({ x: 1279, y: 799 })
  })

  it('letterboxes a box wider than the frame and refuses points in the bars', () => {
    const box = { left: 0, top: 0, width: 1000, height: 400 }
    // 1280x800 fits as 640x400, centred at left 180.
    expect(drawnRect(box, frame)).toEqual({ left: 180, top: 0, width: 640, height: 400 })
    expect(mapPointToFrame(100, 200, box, frame)).toBeNull()
    expect(mapPointToFrame(180, 200, box, frame)).toEqual({ x: 0, y: 400 })
    expect(mapPointToFrame(500, 200, box, frame)).toEqual({ x: 640, y: 400 })
  })

  it('letterboxes a box taller than the frame', () => {
    const box = { left: 0, top: 0, width: 640, height: 1000 }
    expect(drawnRect(box, frame)).toEqual({ left: 0, top: 300, width: 640, height: 400 })
    expect(mapPointToFrame(320, 100, box, frame)).toBeNull()
    expect(mapPointToFrame(320, 500, box, frame)).toEqual({ x: 640, y: 400 })
  })

  it('returns null for an empty box', () => {
    expect(mapPointToFrame(5, 5, { left: 0, top: 0, width: 0, height: 0 }, frame)).toBeNull()
  })
})

describe('modifiersOf', () => {
  it('builds the CDP bitmask', () => {
    expect(modifiersOf(noMods)).toBe(0)
    expect(modifiersOf({ ...noMods, altKey: true })).toBe(MOD_ALT)
    expect(modifiersOf({ ...noMods, ctrlKey: true, shiftKey: true })).toBe(MOD_CTRL | MOD_SHIFT)
    expect(modifiersOf({ altKey: true, ctrlKey: true, metaKey: true, shiftKey: true })).toBe(MOD_ALT | MOD_CTRL | MOD_META | MOD_SHIFT)
  })
})

describe('pointerToEvent', () => {
  const box = { left: 0, top: 0, width: 640, height: 400 }
  it('maps a left down with a click count', () => {
    expect(pointerToEvent('down', { ...noMods, clientX: 320, clientY: 200, button: 0 }, box, frame, 2))
      .toEqual({ type: 'mouse', action: 'down', x: 640, y: 400, button: 'left', clicks: 2, modifiers: 0, w: 1280 })
  })
  it('carries the width of the frame it mapped against, so the gateway scales by THAT frame', () => {
    // RM-1: the stream mixes frame widths; the gateway's newest frame may be another one.
    const small = { w: 640, h: 400 }
    expect(pointerToEvent('down', { ...noMods, clientX: 320, clientY: 200, button: 0 }, box, small))
      .toMatchObject({ x: 320, y: 200, w: 640 })
  })
  it('names the right and middle buttons', () => {
    expect(pointerToEvent('up', { ...noMods, clientX: 0, clientY: 0, button: 2 }, box, frame)!.button).toBe('right')
    expect(pointerToEvent('up', { ...noMods, clientX: 0, clientY: 0, button: 1 }, box, frame)!.button).toBe('middle')
  })
  it('a move reports the held button from the buttons bitmask and no clicks', () => {
    expect(pointerToEvent('move', { ...noMods, clientX: 10, clientY: 10, button: 0, buttons: 1 }, box, frame))
      .toMatchObject({ action: 'move', button: 'left', clicks: 0 })
    expect(pointerToEvent('move', { ...noMods, clientX: 10, clientY: 10, button: 0, buttons: 0 }, box, frame)!.button).toBe('none')
  })
  it('clamps the click count to the range the browser understands', () => {
    expect(pointerToEvent('down', { ...noMods, clientX: 1, clientY: 1, button: 0 }, box, frame, 0)!.clicks).toBe(1)
    expect(pointerToEvent('down', { ...noMods, clientX: 1, clientY: 1, button: 0 }, box, frame, 9)!.clicks).toBe(3)
  })
  it('drops a pointer in the letterbox', () => {
    expect(pointerToEvent('down', { ...noMods, clientX: 10, clientY: 10, button: 0 }, { left: 0, top: 0, width: 1000, height: 400 }, frame)).toBeNull()
  })
  it('a release in the letterbox or past the canvas is clamped to the frame edge, never dropped', () => {
    // Pointer capture delivers the up wherever the drag ended; a dropped up would
    // leave the page's button held. 1280x800 is drawn at left 180..820 in this box.
    const wide = { left: 0, top: 0, width: 1000, height: 400 }
    expect(pointerToEvent('up', { ...noMods, clientX: 10, clientY: 200, button: 0 }, wide, frame))
      .toMatchObject({ action: 'up', x: 0, y: 400, button: 'left', clicks: 1 })
    expect(pointerToEvent('up', { ...noMods, clientX: 5000, clientY: -40, button: 0 }, wide, frame))
      .toMatchObject({ action: 'up', x: 1279, y: 0 })
  })
  it('a move with a button held is clamped too, so the drag follows to the edge; a hover outside is dropped', () => {
    const wide = { left: 0, top: 0, width: 1000, height: 400 }
    expect(pointerToEvent('move', { ...noMods, clientX: 10, clientY: 200, button: 0, buttons: 1 }, wide, frame))
      .toMatchObject({ action: 'move', x: 0, y: 400, button: 'left' })
    expect(pointerToEvent('move', { ...noMods, clientX: 10, clientY: 200, button: 0, buttons: 0 }, wide, frame)).toBeNull()
  })
})

describe('wheelToEvent', () => {
  const box = { left: 0, top: 0, width: 640, height: 400 }
  it('passes pixel deltas through, rounded', () => {
    expect(wheelToEvent({ ...noMods, clientX: 320, clientY: 200, deltaX: 0, deltaY: 53.4, deltaMode: 0 }, box, frame))
      .toEqual({ type: 'wheel', x: 640, y: 400, dx: 0, dy: 53, modifiers: 0, w: 1280 })
  })
  it('scales line and page deltas to pixels', () => {
    expect(wheelToEvent({ ...noMods, clientX: 0, clientY: 0, deltaX: 0, deltaY: 3, deltaMode: 1 }, box, frame)!.dy).toBe(48)
    expect(wheelToEvent({ ...noMods, clientX: 0, clientY: 0, deltaX: 0, deltaY: 1, deltaMode: 2 }, box, frame)!.dy).toBe(800)
  })
})

describe('keyToEvent', () => {
  it('carries text on a printable key down only', () => {
    expect(keyToEvent('down', { ...noMods, key: 'a', code: 'KeyA' })).toEqual({ type: 'key', action: 'down', key: 'a', code: 'KeyA', modifiers: 0, text: 'a' })
    expect(keyToEvent('up', { ...noMods, key: 'a', code: 'KeyA' })).not.toHaveProperty('text')
    expect(keyToEvent('down', { ...noMods, key: 'Enter', code: 'Enter' })).not.toHaveProperty('text')
  })
  it('drops the text under Ctrl or Meta, which are shortcuts, not typing', () => {
    expect(keyToEvent('down', { ...noMods, ctrlKey: true, key: 'a', code: 'KeyA' })).not.toHaveProperty('text')
    expect(keyToEvent('down', { ...noMods, metaKey: true, key: 'v', code: 'KeyV' })).not.toHaveProperty('text')
    expect(keyToEvent('down', { ...noMods, shiftKey: true, key: 'A', code: 'KeyA' }).text).toBe('A')
  })
  it('Esc is the hand-back key and nothing else is', () => {
    expect(isHandBackKey('Escape')).toBe(true)
    expect(isHandBackKey('Enter')).toBe(false)
  })
})

describe('touch typing', () => {
  it('reads the focus reply and refuses anything malformed', () => {
    expect(focusOfReply({ sent: 1, focus: { editable: true, kind: 'tel', mode: '' } })).toEqual({ editable: true, kind: 'tel', mode: '' })
    expect(focusOfReply({ sent: 1 })).toBeNull()
    expect(focusOfReply(undefined)).toBeNull()
    expect(focusOfReply({ focus: { editable: 'yes', kind: 3 } })).toEqual({ editable: false, kind: '', mode: '' })
  })

  it('asks for the keyboard the field wants: its own inputmode first, else by type', () => {
    expect(keyboardHints({ editable: true, kind: 'number', mode: '' })).toEqual({ inputMode: 'decimal', enterKeyHint: 'go' })
    expect(keyboardHints({ editable: true, kind: 'tel', mode: '' }).inputMode).toBe('tel')
    expect(keyboardHints({ editable: true, kind: 'text', mode: 'numeric' }).inputMode).toBe('numeric')
    expect(keyboardHints({ editable: true, kind: 'password', mode: '' }).inputMode).toBe('text')
    expect(keyboardHints({ editable: true, kind: 'textarea', mode: '' }).enterKeyHint).toBe('enter')
  })

  it('splits committed text to the wire cap, never inside a surrogate pair', () => {
    const long = '\u{1F600}'.repeat(TEXT_CHUNK + 1)
    const evs = textToEvents(long)
    expect(evs).toHaveLength(2)
    expect(evs.every(e => e.key === '' && e.action === 'press')).toBe(true)
    expect(Array.from(evs[0].text ?? '')).toHaveLength(TEXT_CHUNK)
    expect(textToEvents('')).toEqual([])
  })

  it('diffs the proxy into Backspaces and text, by code point, a newline as Enter', () => {
    const keys = (evs: ReturnType<typeof diffToEvents>) => evs.map(e => e.text !== undefined ? `t:${e.text}` : `${e.action}:${e.key}`)
    expect(keys(diffToEvents('\u200b', '\u200bhi'))).toEqual(['t:hi'])
    expect(keys(diffToEvents('\u200b', ''))).toEqual(['down:Backspace', 'up:Backspace'])
    // Autocorrect / predictive text: "teh" became "the ".
    expect(keys(diffToEvents('\u200bteh', '\u200bthe '))).toEqual(['down:Backspace', 'up:Backspace', 'down:Backspace', 'up:Backspace', 't:he '])
    // An emoji is one character to delete.
    expect(keys(diffToEvents('\u200ba\u{1F600}', '\u200ba'))).toEqual(['down:Backspace', 'up:Backspace'])
    expect(keys(diffToEvents('', 'a\nb'))).toEqual(['t:a', 'down:Enter', 'up:Enter', 't:b'])
    expect(diffToEvents('same', 'same')).toEqual([])
  })

  it('forwards named keys and chords from the proxy, not printable or IME placeholder keys', () => {
    // Backspace edits the proxy, and the edit is what is sent.
    expect(proxyKeyIsForwarded({ ...noMods, key: 'Backspace', code: 'Backspace' })).toBe(false)
    expect(proxyKeyIsForwarded({ ...noMods, key: 'Backspace', code: 'Backspace', ctrlKey: true })).toBe(true)
    expect(proxyKeyIsForwarded({ ...noMods, key: 'Delete', code: 'Delete' })).toBe(true)
    expect(proxyKeyIsForwarded({ ...noMods, key: 'Enter', code: 'Enter' })).toBe(true)
    expect(proxyKeyIsForwarded({ ...noMods, key: 'a', code: 'KeyA', ctrlKey: true })).toBe(true)
    expect(proxyKeyIsForwarded({ ...noMods, key: '5', code: 'Digit5' })).toBe(false)
    expect(proxyKeyIsForwarded({ ...noMods, key: 'Unidentified', code: '', keyCode: 229 })).toBe(false)
    expect(proxyKeyIsForwarded({ ...noMods, key: 'Backspace', code: 'Backspace', isComposing: true })).toBe(false)
  })
})

describe('touch gestures', () => {
  const box = { left: 0, top: 0, width: 640, height: 400 }
  it('a finger inside the slop is a tap, past it a swipe', () => {
    expect(isSwipe({ x: 0, y: 0 }, { x: TAP_SLOP_PX, y: 0 })).toBe(false)
    expect(isSwipe({ x: 0, y: 0 }, { x: 6, y: 6 })).toBe(true)
  })
  it('a swipe is a wheel at the start point: finger up scrolls the page down, scaled to frame pixels', () => {
    // 640x400 CSS box drawing a 1280x800 frame: 2 frame px per CSS px.
    expect(swipeToWheel({ x: 100, y: 200 }, { x: 90, y: 150 }, { x: 100, y: 220 }, box, frame))
      .toEqual({ type: 'wheel', x: 200, y: 440, dx: 20, dy: 100, modifiers: 0, w: 1280 })
    expect(swipeToWheel({ x: 100, y: 100 }, { x: 100, y: 130 }, { x: 100, y: 100 }, box, frame)?.dy).toBe(-60)
  })
  it('no movement, or no box, sends nothing', () => {
    expect(swipeToWheel({ x: 5, y: 5 }, { x: 5, y: 5 }, { x: 5, y: 5 }, box, frame)).toBeNull()
    expect(swipeToWheel({ x: 5, y: 5 }, { x: 5, y: 50 }, { x: 5, y: 5 }, { ...box, width: 0 }, frame)).toBeNull()
  })
})

describe('nextClickCount', () => {
  const press = (t: number, x = 10, y = 10, button = 0, count = 1) => ({ t, x, y, button, count })
  it('a first press is one click', () => {
    expect(nextClickCount(null, 1000, 10, 10, 0)).toBe(1)
  })
  it('a press soon after and near the last one counts up, capped at a triple', () => {
    expect(nextClickCount(press(1000), 1100, 12, 12, 0)).toBe(2)
    expect(nextClickCount(press(1000, 10, 10, 0, 2), 1100, 10, 10, 0)).toBe(3)
    expect(nextClickCount(press(1000, 10, 10, 0, 3), 1100, 10, 10, 0)).toBe(3)
    expect(nextClickCount(press(1000), 1000 + MULTI_CLICK_MS, 10 + MULTI_CLICK_PX, 10, 0)).toBe(2)
  })
  it('too late, too far or another button starts again at one', () => {
    expect(nextClickCount(press(1000), 1000 + MULTI_CLICK_MS + 1, 10, 10, 0)).toBe(1)
    expect(nextClickCount(press(1000), 1100, 10 + MULTI_CLICK_PX + 1, 10, 0)).toBe(1)
    expect(nextClickCount(press(1000), 1100, 10, 10, 2)).toBe(1)
    expect(nextClickCount(press(1000), 900, 10, 10, 0)).toBe(1)
  })
})

describe('isImePlaceholderKey', () => {
  it('an IME placeholder key is one; a real key is not', () => {
    expect(isImePlaceholderKey({ ...noMods, key: 'Process', code: 'KeyN' })).toBe(true)
    expect(isImePlaceholderKey({ ...noMods, key: 'n', code: 'KeyN', keyCode: 229 })).toBe(true)
    expect(isImePlaceholderKey({ ...noMods, key: 'n', code: 'KeyN', isComposing: true })).toBe(true)
    expect(isImePlaceholderKey({ ...noMods, key: 'Enter', code: 'Enter', keyCode: 13 })).toBe(false)
  })
})

describe('desktop proxy keys', () => {
  it('every real key is forwarded, printable ones and Backspace too; IME and dead keys are not', () => {
    expect(desktopProxyKeyIsForwarded({ ...noMods, key: 'j', code: 'KeyJ', keyCode: 74 })).toBe(true)
    expect(desktopProxyKeyIsForwarded({ ...noMods, key: 'Backspace', code: 'Backspace', keyCode: 8 })).toBe(true)
    expect(desktopProxyKeyIsForwarded({ ...noMods, key: 'Enter', code: 'Enter', keyCode: 13 })).toBe(true)
    expect(desktopProxyKeyIsForwarded({ ...noMods, key: 'Process', code: 'KeyN', keyCode: 229 })).toBe(false)
    expect(desktopProxyKeyIsForwarded({ ...noMods, key: 'n', code: 'KeyN', isComposing: true })).toBe(false)
    expect(desktopProxyKeyIsForwarded({ ...noMods, key: 'Dead', code: 'KeyE', altKey: true })).toBe(false)
  })
  it('a release repeats its press without the text', () => {
    expect(keyUpFor({ type: 'key', action: 'down', key: 'a', code: 'KeyA', text: 'a', modifiers: 8 }))
      .toEqual({ type: 'key', action: 'up', key: 'a', code: 'KeyA', modifiers: 8 })
  })
})
