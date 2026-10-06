import { screen, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import WelcomeView from './WelcomeView'
import { MemoryModeChip } from './MemoryModeChip'
import { api } from '../api/client'
import { getThemeBranding } from '../themeBranding'
import { i18nT } from '../i18n/t'
import { attachReport, sendErrorToChat } from '../utils/errorReport'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, suggestions: vi.fn() } }
})

vi.mock('../utils/errorReport', async importOriginal => {
  const mod = await importOriginal<typeof import('../utils/errorReport')>()
  return { ...mod, sendErrorToChat: vi.fn(() => true) }
})

vi.mock('../themeBranding', async importOriginal => {
  const mod = await importOriginal<typeof import('../themeBranding')>()
  return { ...mod, getThemeBranding: vi.fn(() => undefined) }
})

const suggestions = vi.mocked(api.suggestions)
const branding = vi.mocked(getThemeBranding)

type Suggestions = Awaited<ReturnType<typeof api.suggestions>>

const payload = (list: Suggestions['suggestions']): Suggestions => ({
  suggestions: list,
  generated_at: 1,
  stale: false,
})

const chooserTrigger = () =>
  screen.getByText(i18nT('components.welcomeView.choose_memory_mode')).closest('button')!
const temporaryUndoTrigger = () =>
  screen.getByText(
    i18nT('components.welcomeView.temporary_active_switch_to_persistent'),
  ).closest('button')!

describe('WelcomeView', () => {
  beforeEach(() => {
    suggestions.mockReset()
    suggestions.mockResolvedValue(payload([]))
    branding.mockReset()
    branding.mockReturnValue(undefined)
  })

  it('falls back to the built-in pills when the API returns none', async () => {
    const setInput = vi.fn()
    renderWithProviders(<WelcomeView setInput={setInput} />)

    const pill = await screen.findByRole('button', {
      name: i18nT('components.welcomeView.suggestion_search_code'),
    })
    fireEvent.click(pill)
    expect(setInput).toHaveBeenCalledWith(i18nT('components.welcomeView.suggestion_search_code'))
  })

  it('falls back to the built-in pills when every server suggestion is malformed', async () => {
    suggestions.mockResolvedValue(payload([null, { text: 42 }] as unknown as Suggestions['suggestions']))
    renderWithProviders(<WelcomeView setInput={vi.fn()} />)

    expect(await screen.findByRole('button', {
      name: i18nT('components.welcomeView.suggestion_search_code'),
    })).toBeInTheDocument()
  })

  it('prefers the server suggestions and feeds a clicked pill to the composer', async () => {
    suggestions.mockResolvedValue(payload(['zzq alpha', 'zzq beta']))
    const setInput = vi.fn()
    renderWithProviders(<WelcomeView setInput={setInput} />)

    fireEvent.click(await screen.findByRole('button', { name: 'zzq alpha' }))
    expect(setInput).toHaveBeenCalledWith('zzq alpha')
    // mousedown is suppressed so the pill never takes focus
    expect(fireEvent.mouseDown(screen.getByRole('button', { name: 'zzq beta' }))).toBe(false)
  })

  it('renders {text, kind} items with their kind and legacy strings as general', async () => {
    suggestions.mockResolvedValue(payload([
      { text: 'zzq code', kind: 'code' },
      'zzq legacy',
      { text: 'zzq weird', kind: 'nope' },
    ]))
    const setInput = vi.fn()
    renderWithProviders(<WelcomeView setInput={setInput} />)

    const code = await screen.findByRole('button', { name: 'zzq code' })
    expect(code).toHaveAttribute('data-kind', 'code')
    expect(screen.getByRole('button', { name: 'zzq legacy' })).toHaveAttribute('data-kind', 'general')
    expect(screen.getByRole('button', { name: 'zzq weird' })).toHaveAttribute('data-kind', 'general')
    fireEvent.click(code)
    expect(setInput).toHaveBeenCalledWith('zzq code')
  })

  it('offers no refresh control: the suggestion list is fetched once and never forced', async () => {
    suggestions.mockResolvedValue(payload(['zzq only']))
    renderWithProviders(<WelcomeView setInput={vi.fn()} />)
    await screen.findByRole('button', { name: 'zzq only' })

    expect(screen.getAllByRole('button')).toHaveLength(1)
    expect(suggestions).toHaveBeenCalledTimes(1)
    expect(suggestions).not.toHaveBeenCalledWith(true)
  })

  it('a failed suggestions fetch renders an ErrorNotice with the hand-off and keeps the fallback cards', async () => {
    // The transport pins its structured report to the rejection, as the api client does.
    const err = attachReport(new Error('zzq suggestions down'), {
      id: 'zzq-report', at: 1, source: 'api', message: 'zzq suggestions down',
      status: 503, code: 'zzq_suggestions_unavailable',
    })
    suggestions.mockRejectedValue(err)
    vi.mocked(sendErrorToChat).mockClear()
    renderWithProviders(<WelcomeView setInput={vi.fn()} />)

    const notice = await screen.findByRole('alert')
    // The localized line, never the transport error's own text.
    expect(notice).toHaveTextContent(i18nT('components.welcomeView.suggestions_failed_to_load'))
    expect(notice).not.toHaveTextContent('zzq suggestions down')
    expect(
      screen.getByRole('button', { name: i18nT('components.welcomeView.suggestion_search_code') }),
    ).toBeInTheDocument()

    // The hand-off still carries the structured report, not just the localized line.
    fireEvent.click(within(notice).getByRole('button', { name: i18nT('components.askAgent.ask_the_agent') }))
    expect(sendErrorToChat).toHaveBeenCalledTimes(1)
    const prompt = vi.mocked(sendErrorToChat).mock.calls[0][0]
    expect(prompt).toContain('503')
    expect(prompt).toContain('zzq_suggestions_unavailable')
  })

  it('renders the theme logo instead of the stock ghost when one is registered', () => {
    branding.mockReturnValue({ logo: '/zzq-logo.png' })
    const { container } = renderWithProviders(<WelcomeView setInput={vi.fn()} />)
    expect(container.querySelector('img[src="/zzq-logo.png"]')).toBeTruthy()
  })

  it('never renders the memory chooser (ChatPage puts it above the composer)', () => {
    renderWithProviders(<WelcomeView setInput={vi.fn()} />)
    expect(
      screen.queryByText(i18nT('components.welcomeView.choose_memory_mode')),
    ).not.toBeInTheDocument()
  })
})

describe('MemoryModeChip', () => {
  it('picks a memory mode from the popover and closes it', () => {
    const onSwitchMode = vi.fn()
    renderWithProviders(
      <MemoryModeChip onSwitchMode={onSwitchMode} />,
    )
    fireEvent.click(chooserTrigger())

    const incognito = screen.getByText(i18nT('components.welcomeView.incognito'))
    fireEvent.click(incognito.closest('button')!)
    expect(onSwitchMode).toHaveBeenCalledWith('incognito')
    expect(
      screen.queryByText(i18nT('components.welcomeView.incognito')),
    ).not.toBeInTheDocument()
  })

  it('offers exactly the two memory modes and nothing else', () => {
    renderWithProviders(
      <MemoryModeChip onSwitchMode={vi.fn()} />,
    )
    fireEvent.click(chooserTrigger())
    const popover = screen
      .getByText(i18nT('components.welcomeView.incognito'))
      .closest('div.fixed')!
    const cards = Array.from(popover.querySelectorAll('button')).map(b => b.textContent)
    expect(cards).toHaveLength(2)
    expect(cards[0]).toContain(i18nT('components.welcomeView.incognito'))
    expect(cards[0]).toContain(
      'Uses existing memory but learns no lessons. Keeps the transcript for tab recovery.',
    )
    expect(cards[1]).toContain(i18nT('components.welcomeView.temporary'))
    expect(cards[1]).toContain(
      'Uses no memory and learns no lessons. Keeps the transcript for tab recovery.',
    )
  })

  it('an outside mousedown closes the popover, one inside keeps it', () => {
    renderWithProviders(
      <MemoryModeChip onSwitchMode={vi.fn()} />,
    )
    fireEvent.click(chooserTrigger())

    fireEvent.mouseDown(screen.getByText(i18nT('components.welcomeView.incognito')))
    expect(screen.getByText(i18nT('components.welcomeView.incognito'))).toBeInTheDocument()

    fireEvent.mouseDown(document.body)
    expect(
      screen.queryByText(i18nT('components.welcomeView.incognito')),
    ).not.toBeInTheDocument()
  })

  it('resets the memory mode from the ephemeral trigger', () => {
    const onSwitchMode = vi.fn()
    renderWithProviders(
      <MemoryModeChip memoryMode="temporary" onSwitchMode={onSwitchMode} />,
    )
    fireEvent.click(temporaryUndoTrigger())
    expect(onSwitchMode).toHaveBeenCalledWith('persistent')
  })
})
