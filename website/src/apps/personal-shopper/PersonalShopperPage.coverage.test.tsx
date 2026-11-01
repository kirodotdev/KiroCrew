import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import PersonalShopperPage from './PersonalShopperPage'
import { createSlot } from '../../store/chatSlice'
import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'

const { navigateMock } = vi.hoisted(() => ({ navigateMock: vi.fn() }))

vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigateMock,
}))

vi.mock('../../store/chatSlice', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../store/chatSlice')>()),
  createSlot: vi.fn(),
}))

vi.mock('./PreferencesTab', async () => {
  const React = await import('react')
  return { PreferencesTab: () => React.createElement('div', null, 'preferences-stub') }
})

vi.mock('./HistoryTab', async () => {
  const React = await import('react')
  return { HistoryTab: () => React.createElement('div', null, 'history-stub') }
})

vi.mock('./SitesTab', async () => {
  const React = await import('react')
  return { SitesTab: () => React.createElement('div', null, 'sites-stub') }
})

const mockCreateSlot = vi.mocked(createSlot)

const startLabel = () => i18nT('apps.personalShopper.personalShopperPage.start_advisor_conversation')
const tabLabel = (key: string) => i18nT(`apps.personalShopper.personalShopperPage.${key}`)

/** A thunk whose `unwrap()` settles with the given promise, the way `dispatch(createSlot(...)).unwrap()` does. */
function thunkResolving(settle: () => Promise<unknown>) {
  return () => ({ unwrap: settle })
}

/** Narrow viewports collapse the segments into a dropdown: open it on the current tab, then pick the next one. */
function pickTab(current: string, next: string) {
  if (!screen.queryByRole('radio', { name: tabLabel(next) })) {
    fireEvent.click(screen.getByRole('button', { name: tabLabel(current) }))
  }
  fireEvent.click(screen.getByRole('radio', { name: tabLabel(next) }))
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('PersonalShopperPage', () => {
  it('opens a persistent advisor slot without a session folder, then navigates to chat', async () => {
    mockCreateSlot.mockImplementation(() => thunkResolving(() => Promise.resolve({})) as never)
    renderWithProviders(<PersonalShopperPage />)

    fireEvent.click(screen.getByRole('button', { name: startLabel() }))

    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/chat'))
    expect(mockCreateSlot).toHaveBeenCalledWith({
      agent: 'personal-shopper-advisor',
      memory_mode: 'persistent',
      sessionFolder: false,
    })
  })

  it('keeps the user on the page and shows the error code when the slot cannot be created', async () => {
    mockCreateSlot.mockImplementation(
      () => thunkResolving(() => Promise.reject(new Error('gateway_unreachable'))) as never,
    )
    renderWithProviders(<PersonalShopperPage />)

    fireEvent.click(screen.getByRole('button', { name: startLabel() }))

    expect(await screen.findByText(/gateway_unreachable/)).toBeInTheDocument()
    expect(navigateMock).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: startLabel() })).toBeEnabled()
  })

  it('shows the shared unexpected-error copy when the rejection carries no message', async () => {
    mockCreateSlot.mockImplementation(() => thunkResolving(() => Promise.reject(undefined)) as never)
    renderWithProviders(<PersonalShopperPage />)

    fireEvent.click(screen.getByRole('button', { name: startLabel() }))

    const fallback = i18nT('components.errorBoundary.something_went_wrong')
    expect(await screen.findByText(fallback, { exact: false })).toBeInTheDocument()
    expect(navigateMock).not.toHaveBeenCalled()
  })

  it('disables the start button while the slot is being created', async () => {
    let finish: () => void = () => {}
    mockCreateSlot.mockImplementation(
      () =>
        thunkResolving(
          () =>
            new Promise((resolve) => {
              finish = () => resolve({})
            }),
        ) as never,
    )
    renderWithProviders(<PersonalShopperPage />)

    fireEvent.click(screen.getByRole('button', { name: startLabel() }))

    expect(await screen.findByRole('button', { name: startLabel() })).toBeDisabled()
    finish()
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/chat'))
  })

  it('shows the preferences tab first and switches to history and sites on selection', async () => {
    renderWithProviders(<PersonalShopperPage />)

    expect(screen.getByText('preferences-stub')).toBeInTheDocument()
    expect(screen.queryByText('history-stub')).not.toBeInTheDocument()

    pickTab('tab_preferences', 'tab_history')
    expect(await screen.findByText('history-stub')).toBeInTheDocument()
    expect(screen.queryByText('preferences-stub')).not.toBeInTheDocument()

    pickTab('tab_history', 'tab_sites')
    expect(await screen.findByText('sites-stub')).toBeInTheDocument()
    expect(screen.queryByText('history-stub')).not.toBeInTheDocument()
  })
})
