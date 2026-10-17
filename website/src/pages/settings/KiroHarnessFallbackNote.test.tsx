/**
 * Settings → Chat → Model fallback: the note that the Kiro CLI and kiro-agent
 * harnesses apply the app.kiro.dev fallbacks first appears only on
 * those harnesses. Other harnesses have no engine fallback, so the note would
 * mislead there and is not rendered.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import React from 'react'
import { KiroHarnessFallbackNote, KIRO_MODEL_SETTINGS_URL } from './KiroHarnessFallbackNote'

describe('KiroHarnessFallbackNote', () => {
  it('renders on the kiro-cli backend with the app.kiro.dev link', () => {
    render(<KiroHarnessFallbackNote cfg={{ agent: { acp_backend: 'kiro' } }} />)
    const note = screen.getByTestId('kiro-harness-fallback-note')
    expect(note).toBeTruthy()
    const link = note.querySelector('a')
    expect(link?.getAttribute('href')).toBe(KIRO_MODEL_SETTINGS_URL)
    expect(link?.getAttribute('target')).toBe('_blank')
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('renders on the kas backend', () => {
    render(<KiroHarnessFallbackNote cfg={{ agent: { acp_backend: 'kas' } }} />)
    expect(screen.getByTestId('kiro-harness-fallback-note')).toBeTruthy()
  })

  it('renders when the loaded config holds no choice, which resolves to the default', () => {
    render(<KiroHarnessFallbackNote cfg={{}} />)
    expect(screen.getByTestId('kiro-harness-fallback-note')).toBeTruthy()
  })

  it('renders nothing before the config has loaded', () => {
    render(<KiroHarnessFallbackNote cfg={undefined} />)
    expect(screen.queryByTestId('kiro-harness-fallback-note')).toBeNull()
  })

  it.each(['claude', 'codex', 'opencode', 'goose'])('renders nothing on the %s backend', (backend) => {
    render(<KiroHarnessFallbackNote cfg={{ agent: { acp_backend: backend } }} />)
    expect(screen.queryByTestId('kiro-harness-fallback-note')).toBeNull()
  })
})
