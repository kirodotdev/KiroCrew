import { afterEach, describe, expect, it } from 'vitest'

import {
  BARE_CODE_LABEL_KEY,
  bareCodeLabelKeys,
  bindingCaps,
  bindingLabel,
  clampHoldMs,
  defaultBinding,
  defaultConfig,
  displayKey,
  HOLD_MS_DEFAULT,
  HOLD_MS_MAX,
  HOLD_MS_MIN,
  isBareModifier,
  loadPttConfig,
  matchesBinding,
  normalizeBinding,
  normalizeConfig,
  PTT_CHANGED_EVENT,
  PTT_STORAGE_KEY,
  savePttConfig,
  SELECTABLE_BARE_CODES,
  stillHeld,
  toSeconds,
} from './pushToTalk'

afterEach(() => {
  localStorage.clear()
})

/** A KeyboardEvent-shaped stub. Modifier flags default to "nothing else held". */
function key(code: string, mods: Partial<Record<'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey', boolean>> = {}) {
  return { code, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...mods }
}

describe('platform defaults', () => {
  it('gives macOS bare right Option', () => {
    expect(defaultBinding(true)).toEqual({ code: 'AltRight' })
    expect(isBareModifier(defaultBinding(true))).toBe(true)
  })

  // Right Alt is AltGr on most non-mac layouts (reports ctrl+alt, composes
  // characters) and lone left Alt opens the window menu, so those platforms
  // cannot use the mac default.
  it('gives Windows/Linux the Alt+Shift+Space chord instead of a bare Alt', () => {
    const b = defaultBinding(false)
    expect(b).toEqual({ code: 'Space', alt: true, shift: true })
    expect(isBareModifier(b)).toBe(false)
  })

  it('defaults to hybrid mode at 500ms', () => {
    expect(defaultConfig(true)).toEqual({ mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 })
    expect(HOLD_MS_DEFAULT).toBe(500)
  })

  it('offers a label key for every selectable bare code', () => {
    for (const code of SELECTABLE_BARE_CODES) {
      expect(BARE_CODE_LABEL_KEY[code], code).toBeTruthy()
    }
  })

  // ⌃A / ⌃E / ⌃K are live text-editing bindings in every macOS text field.
  it('does not offer left Control', () => {
    expect(SELECTABLE_BARE_CODES).not.toContain('ControlLeft')
  })
})

describe('matchesBinding — bare modifier', () => {
  const b = { code: 'AltRight' }

  it('arms when the modifier is held alone', () => {
    // The keydown for AltRight carries altKey itself, so altKey:true is the
    // "held alone" case, not a second modifier.
    expect(matchesBinding(key('AltRight', { altKey: true }), b)).toBe(true)
  })

  it('ignores the other side of the same key', () => {
    expect(matchesBinding(key('AltLeft', { altKey: true }), b)).toBe(false)
  })

  // Without the family check, code alone would arm here — AltRight's keydown
  // reports altKey regardless of what else is down.
  it('does NOT arm when another modifier family is also held', () => {
    expect(matchesBinding(key('AltRight', { altKey: true, ctrlKey: true }), b)).toBe(false)
    expect(matchesBinding(key('AltRight', { altKey: true, metaKey: true }), b)).toBe(false)
    expect(matchesBinding(key('AltRight', { altKey: true, shiftKey: true }), b)).toBe(false)
  })

  it('arms a bare Shift binding despite shiftKey being set by the key itself', () => {
    expect(matchesBinding(key('ShiftRight', { shiftKey: true }), { code: 'ShiftRight' })).toBe(true)
    expect(matchesBinding(key('ShiftRight', { shiftKey: true, altKey: true }), { code: 'ShiftRight' })).toBe(false)
  })
})

describe('matchesBinding — chord', () => {
  const b = { code: 'Space', alt: true, shift: true }

  it('arms on the exact modifier set', () => {
    expect(matchesBinding(key('Space', { altKey: true, shiftKey: true }), b)).toBe(true)
  })

  it('rejects a superset or a subset', () => {
    expect(matchesBinding(key('Space', { altKey: true, shiftKey: true, ctrlKey: true }), b)).toBe(false)
    expect(matchesBinding(key('Space', { altKey: true }), b)).toBe(false)
    expect(matchesBinding(key('Space'), b)).toBe(false)
  })

  it('rejects the right modifiers on the wrong key', () => {
    expect(matchesBinding(key('KeyS', { altKey: true, shiftKey: true }), b)).toBe(false)
  })
})

describe('stillHeld', () => {
  /** A later KeyboardEvent carrying only the modifier flags. */
  const flags = (m: Partial<Record<'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey', boolean>> = {}) =>
    ({ altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...m })

  it('reports live hardware state for a bare modifier', () => {
    expect(stillHeld(flags({ altKey: true }), { code: 'AltRight' })).toBe(true)
    expect(stillHeld(flags(), { code: 'AltRight' })).toBe(false)
  })

  it('reads the flag belonging to the binding’s own family', () => {
    // A different modifier being down says nothing about ours.
    expect(stillHeld(flags({ ctrlKey: true }), { code: 'AltRight' })).toBe(false)
    expect(stillHeld(flags({ ctrlKey: true }), { code: 'ControlRight' })).toBe(true)
    expect(stillHeld(flags({ metaKey: true }), { code: 'MetaLeft' })).toBe(true)
    expect(stillHeld(flags({ shiftKey: true }), { code: 'ShiftRight' })).toBe(true)
    expect(stillHeld(flags({ shiftKey: true }), { code: 'MetaRight' })).toBe(false)
  })

  // A chord's primary key has no modifier flag, so "is it still down" is
  // unanswerable and must not be reported as a release.
  it('assumes still-held for a chord binding', () => {
    expect(stillHeld(flags(), { code: 'Space', alt: true, shift: true })).toBe(true)
  })

  it('assumes still-held for a non-modifier code', () => {
    expect(stillHeld(flags(), { code: 'KeyA', ctrl: true })).toBe(true)
  })
})

describe('clampHoldMs', () => {
  it('clamps to range', () => {
    expect(clampHoldMs(50)).toBe(HOLD_MS_MIN)
    expect(clampHoldMs(99_999)).toBe(HOLD_MS_MAX)
    expect(clampHoldMs(700)).toBe(700)
  })

  it('falls back to the default for non-numeric or non-finite input', () => {
    expect(clampHoldMs('500')).toBe(HOLD_MS_DEFAULT)
    expect(clampHoldMs(NaN)).toBe(HOLD_MS_DEFAULT)
    expect(clampHoldMs(Infinity)).toBe(HOLD_MS_DEFAULT)
    expect(clampHoldMs(undefined)).toBe(HOLD_MS_DEFAULT)
  })

  it('rounds fractional input', () => {
    expect(clampHoldMs(499.6)).toBe(500)
  })
})

describe('normalizeBinding', () => {
  it('keeps a valid bare modifier', () => {
    expect(normalizeBinding({ code: 'MetaRight' }, true)).toEqual({ code: 'MetaRight' })
  })

  it('keeps a valid chord and drops non-true flags', () => {
    expect(normalizeBinding({ code: 'Space', alt: true, shift: 'yes', ctrl: 0 }, true))
      .toEqual({ code: 'Space', alt: true })
  })

  // A bare character key would make the composer untypable.
  it('refuses a non-modifier key with no modifiers', () => {
    expect(normalizeBinding({ code: 'Space' }, true)).toEqual({ code: 'AltRight' })
    expect(normalizeBinding({ code: 'KeyA' }, true)).toEqual({ code: 'AltRight' })
  })

  it('falls back for a missing or non-string code', () => {
    expect(normalizeBinding({}, true)).toEqual({ code: 'AltRight' })
    expect(normalizeBinding({ code: 42 }, true)).toEqual({ code: 'AltRight' })
    expect(normalizeBinding({ code: '' }, true)).toEqual({ code: 'AltRight' })
    expect(normalizeBinding(null, false)).toEqual({ code: 'Space', alt: true, shift: true })
  })
})

describe('normalizeConfig', () => {
  it('coerces field by field so one bad value does not reset the rest', () => {
    expect(normalizeConfig({ mode: 'nonsense', binding: { code: 'MetaRight' }, holdMs: 800 }, true))
      .toEqual({ mode: 'hybrid', binding: { code: 'MetaRight' }, holdMs: 800 })
    expect(normalizeConfig({ mode: 'ptt', binding: { code: 'bogus-shape' }, holdMs: 'x' }, true))
      .toEqual({ mode: 'ptt', binding: { code: 'AltRight' }, holdMs: HOLD_MS_DEFAULT })
  })

  it('accepts every valid mode', () => {
    for (const mode of ['toggle', 'ptt', 'hybrid'] as const) {
      expect(normalizeConfig({ mode }, true).mode).toBe(mode)
    }
  })

  it('falls back entirely for a non-object', () => {
    expect(normalizeConfig('nope', true)).toEqual(defaultConfig(true))
    expect(normalizeConfig(null, true)).toEqual(defaultConfig(true))
  })
})

describe('persistence', () => {
  it('round-trips through localStorage', () => {
    const cfg = { mode: 'ptt' as const, binding: { code: 'ShiftRight' }, holdMs: 300 }
    savePttConfig(cfg)
    expect(loadPttConfig(true)).toEqual(cfg)
  })

  it('broadcasts a change event so other consumers re-read', () => {
    let fired = 0
    const onChange = () => { fired++ }
    window.addEventListener(PTT_CHANGED_EVENT, onChange)
    savePttConfig(defaultConfig(true))
    window.removeEventListener(PTT_CHANGED_EVENT, onChange)
    expect(fired).toBe(1)
  })

  it('falls back to the default on unparseable stored JSON', () => {
    localStorage.setItem(PTT_STORAGE_KEY, '{not json')
    expect(loadPttConfig(true)).toEqual(defaultConfig(true))
  })

  // Per-field coercion: an invalid mode resets, but an out-of-range NUMBER is
  // clamped rather than reset — the user's intent ("shorter") is preserved.
  it('normalizes a stored config written by an older or broken build', () => {
    localStorage.setItem(PTT_STORAGE_KEY, JSON.stringify({ mode: 'hold', holdMs: -5 }))
    expect(loadPttConfig(true)).toEqual({ mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: HOLD_MS_MIN })
  })
})

describe('display', () => {
  it('renders a bare modifier as side + glyph', () => {
    expect(bindingCaps({ code: 'AltRight' }, true)).toEqual(['R \u2325'])
    expect(bindingCaps({ code: 'ShiftLeft' }, true)).toEqual(['L \u21e7'])
    expect(bindingCaps({ code: 'MetaRight' }, false)).toEqual(['R Win'])
  })

  it('renders a chord in canonical modifier order', () => {
    expect(bindingCaps({ code: 'Space', alt: true, shift: true }, true))
      .toEqual(['\u2325', '\u21e7', 'Space'])
    expect(bindingCaps({ code: 'Space', alt: true, shift: true }, false))
      .toEqual(['Alt', 'Shift', 'Space'])
  })

  it('maps codes to keycap labels', () => {
    expect(displayKey('KeyQ')).toBe('Q')
    expect(displayKey('Digit4')).toBe('4')
    expect(displayKey('Numpad7')).toBe('Num 7')
    expect(displayKey('ArrowLeft')).toBe('\u2190')
    expect(displayKey('Space')).toBe('Space')
  })
})

describe('bindingLabel — spelled out, never the keycap abbreviation', () => {
  // A first-run review found "R ⌥" unreadable: the reader has to infer R=Right
  // on top of ⌥ already being the least-recognised glyph on a Mac keyboard. Any
  // regression back to the abbreviation is a copy regression, so pin it.
  it('never returns the abbreviated side-letter form for a bare modifier', () => {
    for (const code of ['AltRight', 'AltLeft', 'MetaRight', 'ControlRight', 'ShiftRight']) {
      const label = bindingLabel({ code }, true)
      expect(label, code).not.toBe(bindingCaps({ code }, true)[0])
      expect(label, code).not.toMatch(/^[LR] /)
      // Guards a FALSE PASS: an unresolved i18nT returns the dotted catalog key,
      // which would satisfy both assertions above while shipping a raw key to
      // the screen.
      expect(label, code).not.toMatch(/^pages\./)
    }
  })

  it('spells the side out in words', () => {
    expect(bindingLabel({ code: 'AltRight' }, true)).toContain('Right')
    expect(bindingLabel({ code: 'AltLeft' }, true)).toContain('Left')
  })

  // The strip has to be able to NAME whatever the user actually pressed, not
  // just the five keys the dropdown offers.
  it('has a name for all eight modifier codes, not only the selectable five', () => {
    for (const code of [
      'AltLeft', 'AltRight', 'ControlLeft', 'ControlRight',
      'MetaLeft', 'MetaRight', 'ShiftLeft', 'ShiftRight',
    ]) {
      expect(BARE_CODE_LABEL_KEY[code], code).toBeTruthy()
    }
  })

  it('falls back to the keycap sequence for a chord, which has no name', () => {
    expect(bindingLabel({ code: 'Space', alt: true, shift: true }, false)).toBe('Alt + Shift + Space')
  })

  // Spelling the names out shipped Mac vocabulary everywhere: a Windows user was
  // offered "Right Option ⌥" for a key their keyboard labels Alt.
  it('uses the platform keyboard vocabulary, not Mac words everywhere', () => {
    expect(bindingLabel({ code: 'AltRight' }, true)).toContain('Option')
    const win = bindingLabel({ code: 'AltRight' }, false)
    expect(win).toContain('Alt')
    expect(win).not.toContain('Option')
    expect(win).not.toContain('\u2325')

    expect(bindingLabel({ code: 'MetaRight' }, true)).toContain('Command')
    const winMeta = bindingLabel({ code: 'MetaRight' }, false)
    expect(winMeta).not.toContain('Command')
    expect(winMeta).not.toContain('\u2318')
  })

  it('has a non-mac name for all eight modifier codes too', () => {
    for (const code of [
      'AltLeft', 'AltRight', 'ControlLeft', 'ControlRight',
      'MetaLeft', 'MetaRight', 'ShiftLeft', 'ShiftRight',
    ]) {
      expect(bareCodeLabelKeys(false)[code], code).toBeTruthy()
      const label = bindingLabel({ code }, false)
      expect(label, code).not.toMatch(/^pages\./)
    }
  })
})

describe('toSeconds', () => {
  // Milliseconds are a developer unit; every user-facing duration is seconds.
  it('renders one decimal place', () => {
    expect(toSeconds(500)).toBe('0.5')
    expect(toSeconds(1500)).toBe('1.5')
    expect(toSeconds(200)).toBe('0.2')
    expect(toSeconds(1000)).toBe('1.0')
  })

  it('rounds to the nearest tenth rather than truncating', () => {
    expect(toSeconds(857)).toBe('0.9')
    expect(toSeconds(849)).toBe('0.8')
    expect(toSeconds(0)).toBe('0.0')
  })
})
