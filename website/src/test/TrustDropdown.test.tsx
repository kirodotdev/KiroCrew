import { describe, it, expect, vi, afterEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))

import { render, screen, fireEvent } from '@testing-library/react'
import TrustDropdown from '../components/TrustDropdown'
import { i18next } from '../i18n/index'

const btnClass = 'px-2 py-1 rounded text-sm'

afterEach(async () => {
  await i18next.changeLanguage('en')
})

describe('TrustDropdown', () => {
  it('renders closed by default', () => {
    render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell className={btnClass} onAction={() => {}} />)
    expect(screen.getByText('Trust')).toBeInTheDocument()
    expect(screen.queryByText(/Trust all tools/)).not.toBeInTheDocument()
  })

  it('opens on button click', () => {
    render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell className={btnClass} onAction={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    expect(screen.getByText('Trust all tools')).toBeInTheDocument()
  })

  it('shows 3 options for shell command', () => {
    render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell className={btnClass} onAction={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('menuitem')
    const texts = buttons.map(b => b.textContent)
    expect(texts.some(t => t?.includes('ls /tmp'))).toBe(true)
    expect(texts.some(t => t?.includes('ls') && t?.includes('commands'))).toBe(true)
    expect(screen.getByText('Trust all tools')).toBeInTheDocument()
  })

  // The exact-command option is the one the user is most likely to misread: it
  // grants an exact-string match, so a label that renders two different commands
  // identically asks for consent to something unreadable.
  it('does not render two long commands with the same label', () => {
    const shot = (cmd: string) => {
      const { unmount } = render(
        <TrustDropdown fullCommand={cmd} baseCommand="gh" isShell className={btnClass} onAction={() => {}} />,
      )
      fireEvent.click(screen.getByText('Trust'))
      const label = screen.getAllByRole('menuitem')[0].textContent ?? ''
      unmount()
      return label
    }
    const config = shot('gh api repos/owner/some-repository/contents/config.json --jq .sha')
    const secrets = shot('gh api repos/owner/some-repository/contents/secrets.json --jq .sha')
    expect(config).not.toBe(secrets)
  })

  it('carries the untruncated command as a tooltip so nothing is hidden', () => {
    const cmd = 'gh api repos/owner/some-repository/contents/a/very/long/path/to/a/file.json'
    render(<TrustDropdown fullCommand={cmd} baseCommand="gh" isShell className={btnClass} onAction={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    expect(screen.getAllByRole('menuitem')[0].querySelector(`[title="${cmd}"]`)).not.toBeNull()
  })

  it('grants the untruncated command, never the shortened label', () => {
    const cmd = 'gh pr view 42 --repo owner/some-repository --json title,body,comments'
    const onAction = vi.fn()
    render(<TrustDropdown fullCommand={cmd} baseCommand="gh" isShell className={btnClass} onAction={onAction} />)
    fireEvent.click(screen.getByText('Trust'))
    fireEvent.click(screen.getAllByRole('menuitem')[0])
    expect(onAction).toHaveBeenCalledWith('trust_command', cmd)
  })

  // Each locale orders the sentence around the operand differently — Japanese
  // puts it first, German suffixes the base with a hyphen. A fragment pair can
  // only express the English order, so what these pin is that the operand is
  // interpolated INTO the sentence and still renders monospaced.
  it.each([
    ['ja', '「ls /tmp」を信頼', 'ls コマンドをすべて信頼'],
    ['ko', '‘ls /tmp’ 신뢰', 'ls 명령 모두 신뢰'],
    ['de', '„ls /tmp“ vertrauen', 'Allen ls-Befehlen vertrauen'],
    ['zh-CN', '信任“ls /tmp”', '信任所有 ls 命令'],
  ])('places the command inside a whole translated message in %s', async (lng, cmdText, baseText) => {
    await i18next.changeLanguage(lng)
    render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell className={btnClass} onAction={() => {}} />)

    fireEvent.click(screen.getByRole('button'))
    const [commandItem, baseItem] = screen.getAllByRole('menuitem')

    expect(commandItem).toHaveTextContent(cmdText)
    expect(baseItem).toHaveTextContent(baseText)
    expect(commandItem.querySelector('.font-mono')).toHaveTextContent('ls /tmp')
    expect(baseItem.querySelector('.font-mono')).toHaveTextContent('ls')
  })

  it('shows 2 options for non-shell tool', () => {
    render(<TrustDropdown fullCommand="TaskeiGetTask" baseCommand="TaskeiGetTask" isShell={false} className={btnClass} onAction={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    expect(screen.getByText(/TaskeiGetTask/)).toBeInTheDocument()
    expect(screen.getByText('Trust all tools')).toBeInTheDocument()
    expect(screen.queryByText(/commands/)).not.toBeInTheDocument()
  })

  it('calls onAction with trust_command and pattern', () => {
    const onAction = vi.fn()
    render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell className={btnClass} onAction={onAction} />)
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('menuitem')
    const cmdBtn = buttons.find(b => b.textContent?.includes('ls /tmp'))!
    fireEvent.click(cmdBtn)
    expect(onAction).toHaveBeenCalledWith('trust_command', 'ls /tmp')
  })

  it('calls onAction with trust_base and glob pattern', () => {
    const onAction = vi.fn()
    render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell className={btnClass} onAction={onAction} />)
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('menuitem')
    const baseBtn = buttons.find(b => b.textContent?.includes('commands'))!
    fireEvent.click(baseBtn)
    expect(onAction).toHaveBeenCalledWith('trust_base', 'ls *')
  })

  it('calls onAction with trust for entire tool', () => {
    const onAction = vi.fn()
    render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell className={btnClass} onAction={onAction} />)
    fireEvent.click(screen.getByText('Trust'))
    fireEvent.click(screen.getByText('Trust all tools'))
    expect(onAction).toHaveBeenCalledWith('trust')
  })

  it('disables button when disabled prop is true', () => {
    render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell disabled className={btnClass} onAction={() => {}} />)
    expect(screen.getByText('Trust').closest('button')).toBeDisabled()
  })

  it('truncates long command labels', () => {
    const longCmd = 'find /very/long/path/to/directory -name "*.tsx" -exec grep -l something'
    render(<TrustDropdown fullCommand={longCmd} baseCommand="find" isShell className={btnClass} onAction={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    expect(screen.getByText(/…/)).toBeInTheDocument()
  })

  it.skip('closes on outside click — handled by Radix DropdownMenu', () => {
    render(
      <div>
        <div data-testid="outside">outside</div>
        <TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell className={btnClass} onAction={() => {}} />
      </div>,
    )
    fireEvent.click(screen.getByText('Trust'))
    expect(screen.getByText('Trust all tools')).toBeInTheDocument()
    fireEvent.mouseDown(screen.getByTestId('outside'))
    expect(screen.queryByText('Trust all tools')).not.toBeInTheDocument()
  })

  it('closes dropdown after selecting an option', () => {
    render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell className={btnClass} onAction={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    expect(screen.getByText('Trust all tools')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Trust all tools'))
    expect(screen.queryByText('Trust all tools')).not.toBeInTheDocument()
  })

  it('handles multi-binary baseCommand (comma-separated)', () => {
    const onAction = vi.fn()
    render(<TrustDropdown fullCommand="cat /etc/hosts | wc -l" baseCommand="cat,wc" isShell className={btnClass} onAction={onAction} />)
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('menuitem')
    const baseBtn = buttons.find(b => b.textContent?.includes('commands'))!
    expect(baseBtn.textContent).toContain('cat, wc')
    fireEvent.click(baseBtn)
    expect(onAction).toHaveBeenCalledWith('trust_base', 'cat *,wc *')
  })

  it('does not call onAction when disabled and clicked', () => {
    const onAction = vi.fn()
    render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell disabled className={btnClass} onAction={onAction} />)
    fireEvent.click(screen.getByText('Trust'))
    // Dropdown should not open when disabled
    expect(screen.queryByText('Trust all tools')).not.toBeInTheDocument()
    expect(onAction).not.toHaveBeenCalled()
  })

  it('renders Reading prefix as non-shell (2 options)', () => {
    render(<TrustDropdown fullCommand="/home/user/file.txt" baseCommand="/home/user/file.txt" isShell={false} className={btnClass} onAction={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('menuitem')
    const menuButtons = buttons.filter(b => b.textContent !== 'Trust')
    expect(menuButtons.length).toBe(2) // trust_command + trust all
  })

  it('handles empty fullCommand gracefully', () => {
    const onAction = vi.fn()
    render(<TrustDropdown fullCommand="" baseCommand="" isShell={false} className={btnClass} onAction={onAction} />)
    fireEvent.click(screen.getByText('Trust'))
    fireEvent.click(screen.getByText('Trust all tools'))
    expect(onAction).toHaveBeenCalledWith('trust')
  })

  it('trust_command sends exact fullCommand including spaces and flags', () => {
    const onAction = vi.fn()
    render(<TrustDropdown fullCommand="grep -r 'search term' /path/to/dir" baseCommand="grep" isShell className={btnClass} onAction={onAction} />)
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('menuitem')
    const cmdBtn = buttons.find(b => b.textContent?.includes('grep'))!
    fireEvent.click(cmdBtn)
    expect(onAction).toHaveBeenCalledWith('trust_command', "grep -r 'search term' /path/to/dir")
  })
})

describe('TrustDropdown accessibility', () => {
  it('dropdown items are focusable buttons', () => {
    render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell className={btnClass} onAction={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('menuitem')
    // The 3 tier options render as Radix menuitems (the trigger is a
    // separate role=button, not counted here).
    expect(buttons.length).toBeGreaterThanOrEqual(3)
  })

  it('trigger button shows chevron indicator', () => {
    const { container } = render(<TrustDropdown fullCommand="ls" baseCommand="ls" isShell className={btnClass} onAction={() => {}} />)
    // ChevronDown SVG should be present
    expect(container.querySelector('svg')).toBeInTheDocument()
  })
})

describe('TrustDropdown positioning', () => {
  it.skip('renders menu positioned above — handled by Radix Portal', () => {
    render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell className={btnClass} onAction={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    const menu = screen.getByText('Trust all tools').closest('div[class*="absolute"]')
    expect(menu).toBeInTheDocument()
    expect(menu?.className).toContain('bottom-full')
  })
})
