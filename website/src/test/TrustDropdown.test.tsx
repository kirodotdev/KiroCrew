import { describe, it, expect, vi, afterEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))

import { render, screen, fireEvent } from '@testing-library/react'
import TrustDropdown from '../components/TrustDropdown'
// `/all` for the ja/ko/de/zh-CN catalogs: `../i18n` registers English only.
import { i18next } from '../i18n/all'

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

  // The channels surface's `trust` decision is channel-wide and persisted, so
  // it overrides the session-scoped default label with one naming the real
  // grant. These pin both sides: the override renders, and the default is
  // untouched when the prop is absent.
  describe('trustAllLabelKey override', () => {
    const channelKey = 'components.trustDropdown.trust_all_tools_channel'
    const channelLabel = 'Trust all tools in this channel — persists across restarts'

    it('renders the channel-scoped label instead of the default', () => {
      render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell trustAllLabelKey={channelKey} className={btnClass} onAction={() => {}} />)
      fireEvent.click(screen.getByText('Trust'))
      expect(screen.getByText(channelLabel)).toBeInTheDocument()
      // Exact-string match: the default label must not render alongside.
      expect(screen.queryByText('Trust all tools')).not.toBeInTheDocument()
    })

    it('keeps the default label when the prop is absent', () => {
      render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell className={btnClass} onAction={() => {}} />)
      fireEvent.click(screen.getByText('Trust'))
      expect(screen.getByText('Trust all tools')).toBeInTheDocument()
      expect(screen.queryByText(channelLabel)).not.toBeInTheDocument()
    })

    it('still emits the plain trust action under the override', () => {
      const onAction = vi.fn()
      render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell trustAllLabelKey={channelKey} className={btnClass} onAction={onAction} />)
      fireEvent.click(screen.getByText('Trust'))
      fireEvent.click(screen.getByText(channelLabel))
      expect(onAction).toHaveBeenCalledWith('trust')
    })

    it('resolves the override key through the active locale', async () => {
      await i18next.changeLanguage('zh-CN')
      render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell trustAllLabelKey={channelKey} className={btnClass} onAction={() => {}} />)
      fireEvent.click(screen.getByRole('button'))
      expect(screen.getByText('信任此频道中的所有工具 — 重启后仍然有效')).toBeInTheDocument()
    })

    // The shipped channels surface passes both props: no command tiers, and the
    // remaining tier labelled with its real scope. Pinning the combination
    // catches either prop silently disabling the other.
    it('labels the sole remaining tier when the surface has no command', () => {
      const onAction = vi.fn()
      render(<TrustDropdown fullCommand="Researcher" baseCommand="Researcher" isShell={false} hasCommand={false} trustAllLabelKey={channelKey} className={btnClass} onAction={onAction} />)
      fireEvent.click(screen.getByText('Trust'))
      const items = screen.getAllByRole('menuitem')
      expect(items).toHaveLength(1)
      expect(items[0]).toHaveTextContent(channelLabel)
      fireEvent.click(items[0])
      expect(onAction).toHaveBeenCalledWith('trust')
    })
  })

  it('disables button when disabled prop is true', () => {
    render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell disabled className={btnClass} onAction={() => {}} />)
    expect(screen.getByText('Trust').closest('button')).toBeDisabled()
  })

  it('truncates only PATHOLOGICAL command labels — ordinary long ones render whole', () => {
    // The 256 ceiling only guards the menu layout against pathological input
    // (a base64 blob, a megabyte one-liner); a realistic long command renders
    // in full so the user can read exactly what they are trusting.
    const ordinary = 'find /very/long/path/to/directory -name "*.tsx" -exec grep -l something'
    const { unmount } = render(<TrustDropdown fullCommand={ordinary} baseCommand="find" isShell className={btnClass} onAction={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    expect(screen.queryByText(/…/)).not.toBeInTheDocument()
    unmount()

    const pathological = `find ${'/very/long/path/segment'.repeat(12)} -name "*.tsx" -exec grep -l something`
    expect(pathological.length).toBeGreaterThan(256)
    render(<TrustDropdown fullCommand={pathological} baseCommand="find" isShell className={btnClass} onAction={() => {}} />)
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

describe('TrustDropdown without a command (hasCommand=false)', () => {
  // The channels surface titles its approval with an agent ROLE, not a
  // command, and its backend accepts only approved/rejected/trust — so the
  // command-scoped tiers must disappear entirely there.
  it('offers only the plain session-trust action', () => {
    render(<TrustDropdown fullCommand="Researcher" baseCommand="Researcher" isShell={false} hasCommand={false} className={btnClass} onAction={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    const items = screen.getAllByRole('menuitem')
    expect(items).toHaveLength(1)
    expect(items[0].textContent).toContain('Trust all tools')
  })

  it('hides trust_base even when the title happens to look like a shell command', () => {
    render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell hasCommand={false} className={btnClass} onAction={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    const items = screen.getAllByRole('menuitem')
    expect(items).toHaveLength(1)
    expect(items[0].textContent).not.toContain('commands')
    expect(items[0].textContent).toContain('Trust all tools')
  })

  it('emits the plain trust decision with no pattern', () => {
    const onAction = vi.fn()
    render(<TrustDropdown fullCommand="Researcher" baseCommand="Researcher" isShell={false} hasCommand={false} className={btnClass} onAction={onAction} />)
    fireEvent.click(screen.getByText('Trust'))
    fireEvent.click(screen.getByText('Trust all tools'))
    expect(onAction).toHaveBeenCalledTimes(1)
    expect(onAction).toHaveBeenCalledWith('trust')
  })

  it('keeps every tier when hasCommand is not passed (command-bearing surfaces)', () => {
    // Regression guard for the chat surface, which omits the prop: the
    // default must stay true so all tiers keep rendering there.
    render(<TrustDropdown fullCommand="ls /tmp" baseCommand="ls" isShell className={btnClass} onAction={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    expect(screen.getAllByRole('menuitem')).toHaveLength(3)
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
