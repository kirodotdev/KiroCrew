// TelegramPanel is a spec builder for the shared BotChannelPanel. What matters:
// the spec is built PER RENDER (so a language switch re-resolves the catalog),
// it wires Telegram's own API calls, and backtick-delimited runs in the guide
// copy survive as do-not-translate monospace literals.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'

const specs: Record<string, unknown>[] = []

vi.mock('../pages/settings/BotChannelPanel', () => ({
  BotChannelPanel: ({ spec }: { spec: Record<string, unknown> }) => {
    specs.push(spec)
    return (
      <div data-testid="bot-channel-panel">
        <div data-testid="guide-body">{spec.guideBody as ReactNode}</div>
        <div data-testid="forum-toggle-description">
          {(spec.forum as { toggleDescription: ReactNode }).toggleDescription}
        </div>
      </div>
    )
  },
}))

import { TelegramPanel } from '../pages/settings/TelegramPanel'
import { api } from '../api/client'

function renderPanel() {
  specs.length = 0
  render(<TelegramPanel />)
  return specs[0]
}

describe('TelegramPanel', () => {
  it('delegates to the shared bot-channel panel', () => {
    renderPanel()
    expect(screen.getByTestId('bot-channel-panel')).toBeInTheDocument()
  })

  it('identifies the channel and its endpoints', () => {
    const spec = renderPanel()
    expect(spec).toMatchObject({
      name: 'Telegram',
      queryKey: 'telegram-config',
      host: 'api.telegram.org',
      allowlistPlaceholder: '123456789',
      refetchInterval: 15_000,
    })
    expect(spec.getConfig).toBe(api.getTelegramConfig)
    expect(spec.saveConfig).toBe(api.saveTelegramConfig)
    expect(String(spec.setupGuide)).toContain('telegram-integration.md')
  })

  it('points the guide link at BotFather', () => {
    const spec = renderPanel() as { guideLink: { href: string; label: string } }
    expect(spec.guideLink.href).toBe('https://t.me/BotFather')
    expect(spec.guideLink.label.length).toBeGreaterThan(0)
  })

  it('carries the forum sub-spec with a supergroup placeholder', () => {
    const spec = renderPanel() as { forum: Record<string, unknown> }
    expect(spec.forum.allowlistPlaceholder).toBe('-1001234567890')
    expect(String(spec.forum.allowlistLabel).length).toBeGreaterThan(0)
    expect(String(spec.forum.emptyHint).length).toBeGreaterThan(0)
  })

  it('renders backticked runs as monospace literals and strips the delimiters', () => {
    renderPanel()
    const body = screen.getByTestId('guide-body')
    expect(body.textContent).not.toContain('`')
    const mono = body.querySelectorAll('span.font-mono')
    expect(mono.length).toBeGreaterThan(0)
    expect([...mono].map(n => n.textContent)).toContain('/newbot')
  })

  it('applies the same monospace treatment to the forum toggle copy', () => {
    renderPanel()
    const desc = screen.getByTestId('forum-toggle-description')
    expect(desc.textContent).not.toContain('`')
    expect(desc.querySelectorAll('span.font-mono').length).toBeGreaterThan(0)
  })

  it('interpolates the channel command prefix into the shared threshold sentence', () => {
    const spec = renderPanel()
    expect(String(spec.thresholdDescription)).toContain('/compact')
    expect(String(spec.thresholdDescription)).toContain('/new')
  })

  it('rebuilds the spec on every render rather than freezing it at import', () => {
    renderPanel()
    render(<TelegramPanel />)
    expect(specs).toHaveLength(2)
    expect(specs[1]).not.toBe(specs[0])
  })
})
