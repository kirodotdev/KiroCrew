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
    fireEvent.click(screen.getByRole('button', { name: 'Trust all tools' }))
    expect(onApproval).toHaveBeenCalledWith('safe-1', 'trust', undefined, true)
  })
})
