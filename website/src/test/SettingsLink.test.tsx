import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { SettingsLink } from '../components/SettingsLink'
import {
  NavigationLeaveGuardProvider,
  useRegisterNavigationLeaveGuard,
} from '../components/NavigationLeaveGuard'

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
  // A page with a draft publishes a veto through the leave-guard channel; the
  // link is a navigation surface, so it must ask before it unmounts that page.
  function Guarded({ allow, children }: { allow: boolean; children: React.ReactNode }) {
    useRegisterNavigationLeaveGuard(() => allow)
    return <>{children}</>
  }

  it('asks the leave guard on a plain click and stays put when vetoed', () => {
    const onPlainClick = vi.fn()
    render(
      <MemoryRouter initialEntries={['/chat']}>
        <NavigationLeaveGuardProvider>
          <Guarded allow={false}>
            <SettingsLink tab="voice" onPlainClick={onPlainClick}>go</SettingsLink>
          </Guarded>
        </NavigationLeaveGuardProvider>
      </MemoryRouter>,
    )
    const link = screen.getByRole('link', { name: 'go' })
    const ev = fireEvent.click(link)
    // fireEvent returns false when a handler called preventDefault.
    expect(ev).toBe(false)
    expect(onPlainClick).not.toHaveBeenCalled()
  })

  it('runs onPlainClick only for an allowed unmodified primary click', () => {
    const onPlainClick = vi.fn()
    render(
      <MemoryRouter initialEntries={['/chat']}>
        <NavigationLeaveGuardProvider>
          <Guarded allow>
            <SettingsLink tab="voice" onPlainClick={onPlainClick}>go</SettingsLink>
          </Guarded>
        </NavigationLeaveGuardProvider>
      </MemoryRouter>,
    )
    const link = screen.getByRole('link', { name: 'go' })
    // Modified clicks open a new tab: nothing unmounts, so neither the guard
    // nor the host hook is involved.
    fireEvent.click(link, { metaKey: true })
    fireEvent.click(link, { ctrlKey: true })
    fireEvent.click(link, { button: 1 })
    expect(onPlainClick).not.toHaveBeenCalled()
    fireEvent.click(link)
    expect(onPlainClick).toHaveBeenCalledTimes(1)
  })

  it('does not ask the guard when the target is the current address', () => {
    const guard = vi.fn(() => false)
    function Watch({ children }: { children: React.ReactNode }) {
      useRegisterNavigationLeaveGuard(guard)
      return <>{children}</>
    }
    const onPlainClick = vi.fn()
    render(
      <MemoryRouter initialEntries={['/settings/voice']}>
        <NavigationLeaveGuardProvider>
          <Watch>
            <SettingsLink tab="voice" onPlainClick={onPlainClick}>go</SettingsLink>
          </Watch>
        </NavigationLeaveGuardProvider>
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByRole('link', { name: 'go' }))
    expect(guard).not.toHaveBeenCalled()
    expect(onPlainClick).toHaveBeenCalledTimes(1)
  })
})
