import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { SettingsButtonGroup } from '../components/settings'

/**
 * SettingsButtonGroup as a segmented control.
 *
 * The bug this locks: the track used to be `bg-bg-elevated`, and in EVERY
 * light theme `--bg-elevated` and `--card` are the same #ffffff — so the track
 * was invisible against the card and only the selected pill rendered, reading
 * as one stray grey box instead of a three-way choice. The theme-token
 * assertion below is what makes the class assertion a correctness check rather
 * than a style preference: it proves from `index.css` that the OLD token could
 * not have worked.
 */
describe('SettingsButtonGroup — segmented control', () => {
  const base = {
    label: 'Font',
    options: [
      { value: 'sans', label: 'Sans' },
      { value: 'mono', label: 'Mono' },
      { value: 'system', label: 'System' },
    ],
  }

  /** The control's track, addressed by its accessible role + name. */
  const track = () => screen.getByRole('group', { name: 'Font' })

  it('names the group and marks the selected option for screen readers', () => {
    render(<SettingsButtonGroup {...base} value="system" onChange={() => {}} />)

    // Selection is conveyed by elevation, which a screen reader cannot see;
    // aria-pressed is the only channel that carries it.
    expect(screen.getByRole('button', { name: 'System' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Sans' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('button', { name: 'Mono' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('draws the track on a surface distinct from the card', () => {
    render(<SettingsButtonGroup {...base} value="sans" onChange={() => {}} />)

    expect(track().className).toContain('bg-bg-accent')
    expect(track().className).not.toContain('bg-bg-elevated')
  })

  it('raises the selected thumb off the track', () => {
    render(<SettingsButtonGroup {...base} value="mono" onChange={() => {}} />)

    const selected = screen.getByRole('button', { name: 'Mono' })
    // bg-elevated is correct HERE — the thumb sits on the darker track, not on
    // the card, so it is the lighter surface of the pair.
    expect(selected.className).toContain('bg-bg-elevated')
    expect(selected.className).toContain('shadow-sm')
    expect(screen.getByRole('button', { name: 'Sans' }).className).toContain('bg-transparent')
  })

  it('reports the clicked value and leaves state to the caller', () => {
    const onChange = vi.fn()
    render(<SettingsButtonGroup {...base} value="sans" onChange={onChange} />)

    fireEvent.click(screen.getByRole('button', { name: 'System' }))

    expect(onChange).toHaveBeenCalledWith('system')
  })

  it('swallows clicks when disabled', () => {
    const onChange = vi.fn()
    render(<SettingsButtonGroup {...base} value="sans" onChange={onChange} disabled />)

    fireEvent.click(screen.getByRole('button', { name: 'System' }))

    expect(onChange).not.toHaveBeenCalled()
  })

  // The premise of the fix, read from the stylesheet rather than assumed: if a
  // light theme ever gave --bg-elevated its own value, `bg-bg-accent` would be
  // a preference; while they are identical, it is a requirement.
  it('is built on the token identity that made the old track invisible', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf-8')
    // Rule-wise, not line-wise: the light themes share one comma-separated
    // selector list, so splitting on `[data-theme=` tears a single rule apart.
    const rules = [...css.matchAll(/([^{}]*)\{([^{}]*)\}/g)]
    const checked: string[] = []

    for (const [, selector, body] of rules) {
      const elevated = body.match(/--bg-elevated:\s*(#[0-9a-fA-F]+)/)
      const card = body.match(/--card:\s*(#[0-9a-fA-F]+)/)
      if (!elevated || !card) continue
      const lightThemes = [...selector.matchAll(/\[data-theme="([a-z0-9-]+)"\]/g)]
        .map(m => m[1])
        .filter(name => name.endsWith('light'))
      if (lightThemes.length === 0) continue

      checked.push(...lightThemes)
      expect(
        elevated[1].toLowerCase(),
        `${lightThemes.join(',')}: --bg-elevated must equal --card for bg-bg-accent to be required`,
      ).toBe(card[1].toLowerCase())
    }

    // Guard against the regex silently matching nothing and passing vacuously.
    expect(checked.length).toBeGreaterThan(10)
  })
})
