import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { SettingsLink } from '../components/SettingsLink'

function renderLink(ui: React.ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>)
}

describe('SettingsLink', () => {
  it('renders an anchor to the tab route', () => {
    renderLink(<SettingsLink tab="about">Open About</SettingsLink>)
    expect(screen.getByRole('link', { name: 'Open About' })).toHaveAttribute(
      'href',
      '/settings/about',
    )
  })

  it('carries sub and highlight through to the route', () => {
    renderLink(
      <SettingsLink tab="channels" sub="slack" highlight="channels.folder-name-slack">
        Slack folder
      </SettingsLink>,
    )
    expect(screen.getByRole('link', { name: 'Slack folder' })).toHaveAttribute(
      'href',
      '/settings/channels/slack?highlight=channels.folder-name-slack',
    )
  })

  it('defaults to the accent link style and lets className replace it', () => {
    renderLink(<SettingsLink tab="about">styled</SettingsLink>)
    expect(screen.getByRole('link', { name: 'styled' })).toHaveClass('text-accent')

    renderLink(
      <SettingsLink tab="about" className="custom-cls">
        custom
      </SettingsLink>,
    )
    const custom = screen.getByRole('link', { name: 'custom' })
    expect(custom).toHaveClass('custom-cls')
    expect(custom).not.toHaveClass('text-accent')
  })

  it('forwards extra Link props (data-testid, aria)', () => {
    renderLink(
      <SettingsLink tab="about" data-testid="settings-link-x" aria-label="open settings">
        x
      </SettingsLink>,
    )
    expect(screen.getByTestId('settings-link-x')).toHaveAttribute('aria-label', 'open settings')
  })
})
