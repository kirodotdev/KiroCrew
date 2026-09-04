import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import WelcomeView from './WelcomeView'
import { api } from '../api/client'
import { getThemeBranding } from '../themeBranding'
import { i18nT } from '../i18n/t'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, suggestions: vi.fn() } }
})

vi.mock('../themeBranding', async importOriginal => {
  const mod = await importOriginal<typeof import('../themeBranding')>()
  return { ...mod, getThemeBranding: vi.fn(() => undefined) }
})

const suggestions = vi.mocked(api.suggestions)
const branding = vi.mocked(getThemeBranding)

type Suggestions = Awaited<ReturnType<typeof api.suggestions>>

const payload = (list: string[]): Suggestions => ({
  suggestions: list,
  generated_at: 1,
  stale: false,
})

const ephemeralTrigger = () =>
  screen.getByText(i18nT('components.welcomeView.switch_to_ephemeral_mode')).closest('button')!
const undoTrigger = () =>
  screen.getByText(i18nT('components.welcomeView.switch_back_to_default_mode')).closest('button')!

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

  it('prefers the server suggestions and feeds a clicked pill to the composer', async () => {
    suggestions.mockResolvedValue(payload(['zzq alpha', 'zzq beta']))
    const setInput = vi.fn()
    renderWithProviders(<WelcomeView setInput={setInput} />)

    fireEvent.click(await screen.findByRole('button', { name: 'zzq alpha' }))
    expect(setInput).toHaveBeenCalledWith('zzq alpha')
    // mousedown is suppressed so the pill never takes focus
    expect(fireEvent.mouseDown(screen.getByRole('button', { name: 'zzq beta' }))).toBe(false)
  })

  it('the refresh button forces a regeneration and swaps the pills', async () => {
    suggestions.mockResolvedValue(payload(['zzq old']))
    renderWithProviders(<WelcomeView setInput={vi.fn()} />)
    await screen.findByRole('button', { name: 'zzq old' })

    suggestions.mockResolvedValue(payload(['zzq fresh']))
    fireEvent.click(
      screen.getByRole('button', { name: i18nT('components.welcomeView.refresh_suggestions') }),
    )

    expect(await screen.findByRole('button', { name: 'zzq fresh' })).toBeInTheDocument()
    expect(suggestions).toHaveBeenCalledWith(true)
  })

  it('a failed refresh stops spinning and keeps the current pills', async () => {
    suggestions.mockResolvedValue(payload(['zzq kept']))
    renderWithProviders(<WelcomeView setInput={vi.fn()} />)
    await screen.findByRole('button', { name: 'zzq kept' })

    suggestions.mockRejectedValueOnce(new Error('zzq refresh down'))
    const refresh = screen.getByRole('button', {
      name: i18nT('components.welcomeView.refresh_suggestions'),
    })
    await act(async () => { fireEvent.click(refresh) })

    await waitFor(() => expect(refresh).toBeEnabled())
    expect(screen.getByRole('button', { name: 'zzq kept' })).toBeInTheDocument()
  })

  it('orchestrator mode swaps the heading and drops the pills', () => {
    const setInput = vi.fn()
    renderWithProviders(<WelcomeView mode="orchestrator" setInput={setInput} />)

    expect(screen.getByText(i18nT('components.welcomeView.autopilot'))).toBeInTheDocument()
    expect(
      screen.queryByRole('button', {
        name: i18nT('components.welcomeView.refresh_suggestions'),
      }),
    ).not.toBeInTheDocument()

    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('components.welcomeView.try_create_a_plan_to_analyze_kirocrew_code_packa'),
      }),
    )
    expect(setInput).toHaveBeenCalledWith(expect.stringContaining('Create a plan'))
    expect(suggestions).not.toHaveBeenCalled()
  })

  it('renders the theme logo instead of the stock ghost when one is registered', () => {
    branding.mockReturnValue({ logo: '/zzq-logo.png' })
    const { container } = renderWithProviders(
      <WelcomeView mode="orchestrator" setInput={vi.fn()} />,
    )
    expect(container.querySelector('img[src="/zzq-logo.png"]')).toBeTruthy()
  })

  it('hides the ephemeral affordance entirely when neither handler is passed', () => {
    renderWithProviders(<WelcomeView mode="orchestrator" setInput={vi.fn()} />)
    expect(
      screen.queryByText(i18nT('components.welcomeView.switch_to_ephemeral_mode')),
    ).not.toBeInTheDocument()
  })

  it('picks a memory mode from the popover and closes it', () => {
    const onSwitchMode = vi.fn()
    renderWithProviders(
      <WelcomeView mode="orchestrator" setInput={vi.fn()} onSwitchMode={onSwitchMode} />,
    )
    fireEvent.click(ephemeralTrigger())

    const incognito = screen.getByText(i18nT('components.welcomeView.incognito'))
    fireEvent.click(incognito.closest('button')!)
    expect(onSwitchMode).toHaveBeenCalledWith('incognito')
    expect(
      screen.queryByText(i18nT('components.welcomeView.incognito')),
    ).not.toBeInTheDocument()
  })

  it('offers clean as a peer option and turns it on', () => {
    const onToggleClean = vi.fn()
    renderWithProviders(
      <WelcomeView mode="orchestrator" setInput={vi.fn()} onToggleClean={onToggleClean} />,
    )
    fireEvent.click(ephemeralTrigger())
    fireEvent.click(screen.getByText(i18nT('components.welcomeView.clean')).closest('button')!)
    expect(onToggleClean).toHaveBeenCalledWith(true)
  })

  it('an outside mousedown closes the popover, one inside keeps it', () => {
    renderWithProviders(
      <WelcomeView mode="orchestrator" setInput={vi.fn()} onSwitchMode={vi.fn()} />,
    )
    fireEvent.click(ephemeralTrigger())

    fireEvent.mouseDown(screen.getByText(i18nT('components.welcomeView.incognito')))
    expect(screen.getByText(i18nT('components.welcomeView.incognito'))).toBeInTheDocument()

    fireEvent.mouseDown(document.body)
    expect(
      screen.queryByText(i18nT('components.welcomeView.incognito')),
    ).not.toBeInTheDocument()
  })

  it('clearing clean supersedes resetting the memory mode', () => {
    const onSwitchMode = vi.fn()
    const onToggleClean = vi.fn()
    renderWithProviders(
      <WelcomeView
        mode="orchestrator"
        setInput={vi.fn()}
        memoryMode="incognito"
        cleanMode
        onSwitchMode={onSwitchMode}
        onToggleClean={onToggleClean}
      />,
    )
    fireEvent.click(undoTrigger())
    expect(onToggleClean).toHaveBeenCalledWith(false)
    expect(onSwitchMode).not.toHaveBeenCalled()
  })

  it('resets the memory mode when clean is off but the mode is ephemeral', () => {
    const onSwitchMode = vi.fn()
    const onToggleClean = vi.fn()
    renderWithProviders(
      <WelcomeView
        mode="orchestrator"
        setInput={vi.fn()}
        memoryMode="temporary"
        onSwitchMode={onSwitchMode}
        onToggleClean={onToggleClean}
      />,
    )
    fireEvent.click(undoTrigger())
    expect(onSwitchMode).toHaveBeenCalledWith('persistent')
    expect(onToggleClean).not.toHaveBeenCalled()
  })
})
