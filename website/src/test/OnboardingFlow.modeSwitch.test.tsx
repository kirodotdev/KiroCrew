import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import OnboardingFlow from '../components/OnboardingFlow'
import { i18next } from '../i18n/all'
import zh from '../i18n/locales/zh-CN.json'

// The "Pick your look" mode row (System / Light / Dark) names each button from
// the catalog, and marks the chosen mode with aria-pressed.

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kirocrewConfig: vi.fn().mockResolvedValue({
        dashboard: { user_role: '', user_technical_level: '' },
      }),
      patchConfig: vi.fn().mockResolvedValue({}),
      themeBoot: vi.fn().mockResolvedValue({ mode: '', color: '', onboarded: false }),
    },
  }
})

const zhFlow = zh.components.onboardingFlow

function modeGroup() {
  return screen.getByRole('group', { name: i18next.t('components.onboardingFlow.mode_switch_label') })
}

describe('OnboardingFlow mode switch', () => {
  beforeEach(() => { localStorage.removeItem('mc-onboarding-step') })
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('marks only the chosen mode as pressed and follows a click', async () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    const group = modeGroup()
    const dark = within(group).getByRole('button', { name: 'Dark' })
    const light = within(group).getByRole('button', { name: 'Light' })

    fireEvent.click(dark)
    await waitFor(() => expect(dark).toHaveAttribute('aria-pressed', 'true'))
    expect(light).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(light)
    await waitFor(() => expect(light).toHaveAttribute('aria-pressed', 'true'))
    expect(dark).toHaveAttribute('aria-pressed', 'false')
    expect(within(group).getAllByRole('button').filter(b => b.getAttribute('aria-pressed') === 'true')).toHaveLength(1)
  })

  it('names every mode button from the catalog in another language', async () => {
    await i18next.changeLanguage('zh-CN')
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    const names = within(modeGroup()).getAllByRole('button').map(b => b.textContent?.trim())
    expect(names).toEqual([zhFlow.mode_system, zhFlow.mode_light, zhFlow.mode_dark])
    expect(names).not.toContain('System')
    expect(names).not.toContain('Dark')
  })
})
