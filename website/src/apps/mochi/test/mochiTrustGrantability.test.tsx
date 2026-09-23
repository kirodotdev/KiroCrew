import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Bubble } from '../src/renderer/ChatPanel'

afterEach(cleanup)

function approvalMessage(payload: Record<string, unknown>) {
  return {
    id: `approval-${String(payload.id)}`,
    role: 'assistant' as const,
    content: `__approval__${JSON.stringify(payload)}`,
    timestamp: 1,
  }
}

describe('Mochi pending-card trust proof', () => {
  it('hides every Trust control when the server omitted grantability proof', () => {
    render(
      <Bubble
        animate={false}
        message={approvalMessage({
          id: 'redacted-1',
          tool: 'Run hidden command',
          // Even stale/forged scope strings are insufficient without the proof.
          fullCommand: 'echo [REDACTED: credential]',
          baseCommand: 'echo',
        })}
        onApproval={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Trust/ })).not.toBeInTheDocument()
  })

  it('offers Trust only when the permission payload carries server proof', () => {
    const onApproval = vi.fn()
    render(
      <Bubble
        animate={false}
        message={approvalMessage({
          id: 'safe-1',
          tool: 'Run npm test',
          fullCommand: 'npm test',
          baseCommand: 'npm',
          trustGrantable: true,
        })}
        onApproval={onApproval}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Trust' }))
    fireEvent.click(screen.getByRole('button', { name: 'Trust all tools for this session' }))
    expect(onApproval).toHaveBeenCalledWith('safe-1', 'trust', undefined, true)
  })

  it('names the session scope on a proven card with no command scope', () => {
    // The gateway proves a SESSION grant for a card whose command it could not
    // canonicalize, so there is no tier list to open and this one click IS the
    // grant. The button must therefore say what it grants, and the hint must
    // describe the session rather than the pending tool.
    const onApproval = vi.fn()
    render(
      <Bubble
        animate={false}
        message={approvalMessage({
          id: 'scopeless-1',
          tool: 'Run hidden command',
          trustGrantable: true,
        })}
        onApproval={onApproval}
      />,
    )

    expect(screen.queryByRole('button', { name: 'Trust' })).not.toBeInTheDocument()
    const trust = screen.getByRole('button', { name: 'Trust all tools for this session' })
    // No disclosure: with nothing to reveal the button is a direct action.
    expect(trust).not.toHaveAttribute('aria-expanded')
    expect(screen.getByText(/every tool for the rest of this session/i)).toBeInTheDocument()
    expect(screen.queryByText(/Run hidden command from now on/)).not.toBeInTheDocument()

    fireEvent.click(trust)
    expect(onApproval).toHaveBeenCalledWith('scopeless-1', 'trust', undefined, true)
  })

  // The exact-command row grants an exact-STRING match here too, so a run of
  // whitespace inside the command is part of the authority being granted. HTML's
  // default `white-space: normal` collapses runs, which would render
  // `grep "a  b" f` as `grep "a b" f` -- an elision one character wide. The
  // panel's own budget clamp is a separate, deliberate layout decision for a
  // ~240px column; collapsing whitespace earns nothing.
  it('preserves runs of whitespace in the exact-command row', () => {
    const cmd = 'grep -r "two  spaces" /path/to/dir'
    render(
      <Bubble
        animate={false}
        message={approvalMessage({
          id: 'spaced-1',
          tool: 'Run grep',
          fullCommand: cmd,
          baseCommand: 'grep',
          trustGrantable: true,
        })}
        onApproval={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Trust' }))
    // Found by DOM text, not by accessible name: the accessible-name algorithm
    // normalizes whitespace, so the run this test is about is invisible there.
    const row = screen.getAllByRole('button').find(b => b.textContent?.includes(cmd))
    expect(row).toBeDefined()
    const label = row!.querySelector('span')
    expect(label?.style.whiteSpace).toBe('pre-wrap')
  })
})
