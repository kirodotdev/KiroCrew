/**
 * A clickable compiler row is named "Go to line N", and that name replaces the
 * row's content for a screen reader, so the error message must reach it as the
 * row's description.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import '../../i18n/all'
import DiagnosticsList from './DiagnosticsList'
import type { Diagnostic } from './api'

const diag = (over: Partial<Diagnostic>): Diagnostic => ({
  level: 'error',
  message: 'Undefined control sequence',
  file: 'main.tex',
  line: 42,
  ...over,
}) as Diagnostic

describe('papyrus diagnostics rows', () => {
  it('describe a jumpable error by its message', () => {
    render(<DiagnosticsList diagnostics={[diag({})]} log="" onJumpToLine={vi.fn()} />)
    const row = screen.getByRole('button', { name: 'Go to line 42' })
    expect(row).toHaveAccessibleDescription('Undefined control sequence')
  })

  it('give each row its own message', () => {
    render(
      <DiagnosticsList
        diagnostics={[diag({ line: 1, message: 'First problem' }), diag({ level: 'warning', line: 2, message: 'Second problem' })]}
        log=""
        onJumpToLine={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: 'Go to line 1' })).toHaveAccessibleDescription('First problem')
    expect(screen.getByRole('button', { name: 'Go to line 2' })).toHaveAccessibleDescription('Second problem')
  })
})
