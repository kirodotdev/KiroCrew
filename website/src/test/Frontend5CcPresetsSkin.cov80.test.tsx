/**
 * Crew Companion's preset data model, its theme-derived skin, and the switch row.
 *
 * `catPresets` is the gate a user-authored preset passes before it is persisted,
 * plus a registry whose constructor filter is what keeps a corrupted settings
 * file from putting `undefined` in the picker — every interesting line is an
 * error path. `panelSkin.resolveAccentText` is the runtime legibility decision
 * for an arbitrary user theme: it must publish the accent when the accent reads,
 * fall back to the ink colour when it does not, and change nothing at all when
 * the theme has not loaded yet.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import {
  PresetRegistry,
  extractSwatches,
  generatePresetId,
  validatePreset,
  type CatPreset,
} from '../apps/crew-companion/catPresets'
import {
  PANEL_FONT,
  PANEL_RADIUS,
  THEME_SKIN,
  resolveAccentText,
  skinFor,
} from '../apps/crew-companion/panelSkin'
import ToggleRow from '../apps/crew-companion/ToggleRow'

function preset(over: Partial<CatPreset> = {}): CatPreset {
  return {
    id: 'zz-id',
    name: 'zz-name',
    description: 'zz-description',
    colorMap: { '#112233': '#445566' },
    swatches: ['#112233', '#445566'],
    builtIn: true,
    ...over,
  }
}

describe('crew-companion/catPresets — validation', () => {
  it('accepts a well-formed preset', () => {
    expect(validatePreset(preset())).toEqual([])
  })

  it('names every missing or malformed field', () => {
    const errors = validatePreset(
      preset({
        id: '',
        name: '',
        colorMap: { 'nothex': '#445566', '#112233': 'alsonothex' },
        swatches: ['#112233', 'nope'],
      }),
    )
    expect(errors).toContain('id is required')
    expect(errors).toContain('name is required')
    expect(errors.some((e) => e.includes('invalid colorMap key'))).toBe(true)
    expect(errors.some((e) => e.includes('invalid colorMap value'))).toBe(true)
    expect(errors.some((e) => e.includes('invalid swatch'))).toBe(true)
  })

  it('bounds the swatch count at both ends', () => {
    expect(validatePreset(preset({ swatches: ['#112233'] })).join()).toMatch(/swatches length/)
    expect(
      validatePreset(preset({ swatches: ['#111', '#222', '#333', '#444', '#555', '#666'] })).join(),
    ).toMatch(/swatches length/)
    // Exactly at the bounds is fine.
    expect(validatePreset(preset({ swatches: ['#111', '#222'] }))).toEqual([])
    expect(
      validatePreset(preset({ swatches: ['#111', '#222', '#333', '#444', '#555'] })),
    ).toEqual([])
  })
})

describe('crew-companion/catPresets — swatches and ids', () => {
  it('takes the first five distinct valid colours, in order', () => {
    const swatches = extractSwatches({
      a: '#111111',
      b: '#222222',
      c: '#111111',
      d: 'not-a-colour',
      e: '#333333',
      f: '#444444',
      g: '#555555',
      h: '#666666',
    })
    expect(swatches).toEqual(['#111111', '#222222', '#333333', '#444444', '#555555'])
  })

  it('returns nothing when no value is a colour', () => {
    expect(extractSwatches({ a: 'rgb(1,2,3)', b: '' })).toEqual([])
  })

  it('mints unique custom ids', () => {
    const a = generatePresetId()
    const b = generatePresetId()
    expect(a.startsWith('custom-')).toBe(true)
    expect(a).not.toBe(b)
  })
})

describe('crew-companion/catPresets — PresetRegistry', () => {
  const builtIn = preset({ id: 'zz-built' })

  it('drops corrupted custom entries rather than showing them in the picker', () => {
    const reg = new PresetRegistry(
      [builtIn],
      [preset({ id: 'zz-custom', builtIn: false }), null as unknown as CatPreset, preset({ id: '' })],
    )
    expect(reg.getCustomPresets().map((p) => p.id)).toEqual(['zz-custom'])
    expect(reg.getAllPresets().map((p) => p.id)).toEqual(['zz-built', 'zz-custom'])
    expect(reg.getBuiltInPresets().map((p) => p.id)).toEqual(['zz-built'])
  })

  it('tolerates no custom presets at all', () => {
    const reg = new PresetRegistry([builtIn])
    expect(reg.getCustomPresets()).toEqual([])
    expect(reg.getPresetById('zz-built')).toEqual(builtIn)
    expect(reg.getPresetById('zz-missing')).toBeNull()
  })

  it('adds a custom preset under a minted id and marks it not built-in', () => {
    const reg = new PresetRegistry([builtIn])
    const id = reg.addCustomPreset({
      name: 'zz-added',
      description: 'zz',
      colorMap: {},
      swatches: ['#111', '#222'],
    })
    const added = reg.getPresetById(id)
    expect(added?.builtIn).toBe(false)
    expect(added?.name).toBe('zz-added')
  })

  it('refuses to delete a built-in and reports a miss', () => {
    const reg = new PresetRegistry([builtIn], [preset({ id: 'zz-custom', builtIn: false })])
    expect(reg.removeCustomPreset('zz-built')).toBe(false)
    expect(reg.removeCustomPreset('zz-absent')).toBe(false)
    expect(reg.removeCustomPreset('zz-custom')).toBe(true)
    expect(reg.getCustomPresets()).toEqual([])
  })
})

describe('crew-companion/panelSkin', () => {
  it('serves one theme-driven skin regardless of the mode asked for', () => {
    expect(skinFor('dark')).toBe(THEME_SKIN)
    expect(skinFor('light')).toBe(THEME_SKIN)
    expect(skinFor()).toBe(THEME_SKIN)
    // The faint tier is deliberately the same token as muted — a third tier is
    // where this panel's contrast previously failed AA.
    expect(THEME_SKIN.faint).toBe(THEME_SKIN.muted)
    expect(THEME_SKIN.radius).toBe(PANEL_RADIUS.card)
    expect(THEME_SKIN.rowRadius).toBe(PANEL_RADIUS.row)
    expect(PANEL_FONT).toContain('var(')
  })

  /** A host whose custom properties are set inline, so getComputedStyle sees them. */
  function host(props: Record<string, string>) {
    const el = document.createElement('div')
    for (const [k, v] of Object.entries(props)) el.style.setProperty(k, v)
    document.body.appendChild(el)
    return el
  }

  it('publishes the accent as text colour when it is legible on the card', () => {
    const el = host({ '--accent': '#000000', '--card': '#ffffff', '--card-fg': '#333333' })
    resolveAccentText(el)
    expect(el.style.getPropertyValue('--cc-accent-text')).toBe('#000000')
  })

  it('falls back to the ink colour when the accent is not legible', () => {
    const el = host({ '--accent': '#eeeeee', '--card': '#ffffff', '--card-fg': '#111111' })
    resolveAccentText(el)
    expect(el.style.getPropertyValue('--cc-accent-text')).toBe('#111111')
  })

  it('reads the alias tokens when the primary names are absent', () => {
    const el = host({ '--accent': '#000000', '--bg-elevated': '#ffffff', '--text': '#222222' })
    resolveAccentText(el)
    expect(el.style.getPropertyValue('--cc-accent-text')).toBe('#000000')
  })

  it('publishes nothing before the dashboard stylesheet has loaded', () => {
    // The declared fallback in THEME_SKIN must stay in force — writing an empty
    // value here would blank the panel's accent text.
    const el = host({ '--accent': '#000000' })
    resolveAccentText(el)
    expect(el.style.getPropertyValue('--cc-accent-text')).toBe('')
  })
})

describe('crew-companion/ToggleRow', () => {
  it('reports its state through aria-checked and toggles to the opposite', () => {
    const onChange = vi.fn()
    render(<ToggleRow label="zz-label" hint="zz-hint" on={false} onChange={onChange} />)
    const sw = screen.getByRole('switch', { name: 'zz-label' })
    expect(sw.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(sw)
    expect(onChange).toHaveBeenCalledWith(true)
  })

  it('inverts an on switch and shows the hint', () => {
    const onChange = vi.fn()
    render(<ToggleRow label="zz-label" hint="zz-hint" on onChange={onChange} />)
    expect(screen.getByText('zz-hint')).toBeTruthy()
    fireEvent.click(screen.getByRole('switch', { name: 'zz-label' }))
    expect(onChange).toHaveBeenCalledWith(false)
  })

  it('never reports a change while disabled', () => {
    const onChange = vi.fn()
    render(<ToggleRow label="zz-label" hint="zz-hint" on={false} onChange={onChange} disabled />)
    fireEvent.click(screen.getByRole('switch', { name: 'zz-label' }))
    expect(onChange).not.toHaveBeenCalled()
  })
})
