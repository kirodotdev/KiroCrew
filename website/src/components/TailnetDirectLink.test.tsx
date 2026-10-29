import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { TailnetDirectLink } from './TailnetDirectLink'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => false) }))

describe('TailnetDirectLink', () => {
  it('describes Tailscale rules rather than promising invite-only access', () => {
    render(<TailnetDirectLink direct={{ state: 'active', urls: ['https://crew.example.ts.net/'] } as never} />)
    expect(screen.getByTestId('tailnet-direct-link')).toHaveTextContent('By default, anyone allowed by Tailscale access rules')
    expect(screen.getByTestId('tailnet-direct-link')).toHaveTextContent('With local authorization on')
  })

  it('a refused copy shows the shared error notice', async () => {
    render(<TailnetDirectLink direct={{ state: 'active', urls: ['https://crew.example.ts.net/'] } as never} />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    const notice = await screen.findByTestId('direct-link-copy-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(screen.getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
  })

  it('a refused copy offers no hand-off when the host turns it off', async () => {
    render(<TailnetDirectLink direct={{ state: 'active', urls: ['https://crew.example.ts.net/', 'https://100.64.0.1/'] } as never} askAgent={false} />)
    for (const button of screen.getAllByRole('button', { name: 'Copy', hidden: true })) fireEvent.click(button)
    const notices = await screen.findAllByTestId('direct-link-copy-error')
    expect(notices).toHaveLength(2)
    expect(screen.queryByRole('button', { name: /Ask the agent/, hidden: true })).toBeNull()
  })
})
