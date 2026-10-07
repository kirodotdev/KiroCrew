/**
 * Composer chips name what the live session reports running.
 *
 * The model chip leads with the logo of the ACP backend the session runs on,
 * named for screen readers and on hover at every shelf width, and names the
 * model its harness reported, with the selection beside it in the title when
 * the two differ. The context popover, kept inside a phone's viewport, names
 * the served model in full, repeats the backend and says whether Kiro Crew's
 * own tools reached the session, and the agent chip warns when they did not: a
 * session that kept its harness through a config switch, or that runs without
 * the gateway's MCP servers, must not look like any other.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, fireEvent, screen } from '@testing-library/react'
import i18next from 'i18next'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import '../i18n/all'

vi.mock('../api/client', () => ({ api: {} }))

const props = (over: Record<string, unknown> = {}) => ({
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  connected: true,
  agentName: 'reviewer',
  onAgentClick: vi.fn(),
  onProjectClick: vi.fn(),
  onModelClick: vi.fn(),
  modelName: 'global.anthropic.claude-opus-5[1m]',
  contextPct: 12,
  ...over,
})

afterEach(async () => { await i18next.changeLanguage('en') })

describe('ChatInput — what the live session reports', () => {
  it('leads the model chip with the backend logo and names the selection in its title', () => {
    renderWithProviders(
      <ChatInput {...props({ sessionBackend: 'claude', modelSelected: 'global.anthropic.claude-fable-5[1m]' })} />,
    )
    const chip = screen.getByTestId('composer-model-chip')
    const logo = screen.getByTestId('composer-model-chip-backend')
    // A logo left of the model name, not a text label; its name says which harness.
    expect(chip.firstElementChild).toBe(logo)
    expect(logo.textContent).toBe('')
    expect(logo).toHaveAttribute('role', 'img')
    expect(logo).toHaveAttribute('aria-label', 'Claude Code')
    expect(logo).toHaveAttribute('title', 'Claude Code')
    expect(screen.getByTestId('harness-mark-claude')).toBeTruthy()
    // The routing prefix leaves the visible name; the title keeps the full id.
    expect(chip.textContent).toContain('claude-opus-5[1m]')
    expect(chip.textContent).not.toContain('global.anthropic.')
    const title = chip.getAttribute('title') ?? ''
    expect(title).toContain('global.anthropic.claude-opus-5[1m]')
    expect(title).toContain('Selected: global.anthropic.claude-fable-5[1m]')
    expect(title).toContain('Agent harness: Claude Code')
  })

  it('opens the context popover inside a phone viewport when its chip sits at the left edge', () => {
    // At 390px with the chip 24px in, a right-aligned 208px popover began at x=-124.
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 390 })
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue(
      { left: 24, right: 84, top: 790, bottom: 818, width: 60, height: 28, x: 24, y: 790, toJSON: () => ({}) } as DOMRect,
    )
    try {
      renderWithProviders(<ChatInput {...props({ sessionBackend: 'claude' })} />)
      fireEvent.click(screen.getByRole('button', { name: 'Context usage' }))
      const popover = screen.getByTestId('context-usage-popover')
      // Offsets count from the chip's left edge: the box spans x=8 to x=216.
      expect(popover.style.width).toBe('208px')
      expect(popover.style.left).toBe('-16px')
      // The served id in full: a 120px row showed only `global.anthropic…`, and a
      // phone cannot hover a truncated one.
      expect(screen.getByTestId('context-model').textContent).toBe('Modelglobal.anthropic.claude-opus-5[1m]')
    } finally {
      vi.restoreAllMocks()
      Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1024 })
    }
  })

  it('marks kiro-cli, whose backend id is the empty string, with the Kiro ghost', () => {
    renderWithProviders(<ChatInput {...props({ sessionBackend: '' })} />)
    expect(screen.getByTestId('composer-model-chip-backend')).toHaveAttribute('aria-label', 'Kiro CLI')
    expect(screen.getByTestId('harness-mark-kiro')).toBeTruthy()
  })

  it('keeps the backend logo on a phone-width shelf, where labels collapse', () => {
    const observers = new Map<Element, ResizeObserverCallback>()
    vi.stubGlobal('ResizeObserver', class {
      constructor(private callback: ResizeObserverCallback) {}
      observe(target: Element) { observers.set(target, this.callback) }
      unobserve(target: Element) { observers.delete(target) }
      disconnect() {}
    })
    try {
      renderWithProviders(<ChatInput {...props({ sessionBackend: 'codex', modelName: 'gpt-6.1-sol' })} />)
      // A 390px phone leaves the shelf under the 340px compact width.
      const shelf = screen.getByTestId('composer-context-shelf')
      act(() => observers.get(shelf)?.([{
        target: shelf, contentRect: { width: 320, height: 32 },
      } as ResizeObserverEntry], {} as ResizeObserver))
      const logo = screen.getByTestId('composer-model-chip-backend')
      expect(screen.getByTestId('composer-model-chip').firstElementChild).toBe(logo)
      expect(logo).toHaveAttribute('aria-label', 'codex')
      expect(screen.getByTestId('harness-mark-codex')).toBeTruthy()
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('lets the shelf wrap rather than run the missing-tools warning under its neighbours', () => {
    const { unmount } = renderWithProviders(<ChatInput {...props({ sessionBackend: 'claude', gatewayTools: 'not_sent' })} />)
    // At 390px "No tools" ran under the project and context chips: its group now takes the
    // whole line, so the context and model chips wrap below it. `basis-full` rather than
    // `min-w-max`, which also refused to shrink and ran a long crew and project name past
    // the shelf's right edge (`gates2/shelf-width-probe.mjs` measures it in a browser).
    expect(screen.getByTestId('composer-context-shelf').className).toContain('flex-wrap')
    const group = screen.getByTestId('agent-chip-tools-missing').closest('button')!.parentElement!
    expect(group.className).toContain('basis-full')
    expect(group.className).toContain('min-w-0')
    expect(group.className).not.toContain('min-w-max')
    unmount()
    renderWithProviders(<ChatInput {...props({ sessionBackend: 'claude', gatewayTools: 'connected' })} />)
    expect(screen.getByTestId('composer-context-shelf').className).not.toContain('flex-wrap')
  })

  it('names no backend before a session reports one', () => {
    renderWithProviders(<ChatInput {...props({ sessionBackend: null })} />)
    expect(screen.queryByTestId('composer-model-chip-backend')).toBeNull()
  })

  it('warns on the agent chip and in the popover when the gateway tools were not sent', () => {
    renderWithProviders(<ChatInput {...props({ sessionBackend: 'claude', gatewayTools: 'not_sent' })} />)
    expect(screen.getByTestId('agent-chip-tools-missing')).toBeTruthy()
    const agentChip = screen.getByTestId('agent-chip-tools-missing').closest('button')!
    expect(agentChip.getAttribute('aria-label')).toContain('This session has no Kiro Crew tools')
    // Readable without hovering: a phone has no hover to reach the title.
    expect(agentChip.textContent).toContain('No tools')

    fireEvent.click(screen.getByRole('button', { name: 'Context usage' }))
    expect(screen.getByTestId('context-session-backend').textContent).toBe('Agent harnessClaude Code')
    expect(screen.queryByTestId('context-gateway-tools')).toBeNull()
    // What happened, and plainly that nothing here fixes it, rather than a dead end. A
    // warning, not an alert: an agent whose tools list leaves them out chose that.
    const notice = screen.getByTestId('context-gateway-tools-missing')
    expect(notice.textContent).toContain('Kiro Crew tools were not sent to this session, and nothing here can add them.')
    expect(notice).toHaveAttribute('role', 'status')
  })

  it('reports tools that failed to start as an error, not a status row', () => {
    renderWithProviders(<ChatInput {...props({ sessionBackend: '', gatewayTools: 'failed' })} />)
    expect(screen.getByTestId('agent-chip-tools-missing')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Context usage' }))
    expect(screen.queryByTestId('context-gateway-tools')).toBeNull()
    // What happened, then what to do about it.
    const notice = screen.getByTestId('context-gateway-tools-missing')
    expect(notice.textContent).toContain('Kiro Crew tools failed to start in this session. Start a new session to try again.')
    expect(notice).toHaveAttribute('role', 'alert')
  })

  it('does not warn when the gateway tools reached the session', () => {
    renderWithProviders(<ChatInput {...props({ sessionBackend: '', gatewayTools: 'connected' })} />)
    expect(screen.queryByTestId('agent-chip-tools-missing')).toBeNull()
    expect(screen.queryByText('No tools')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Context usage' }))
    expect(screen.getByTestId('context-gateway-tools').textContent).toBe('Kiro Crew toolsAvailable')
  })
})
