import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { PreferencesTab } from './PreferencesTab'
import { SitesTab } from './SitesTab'
import { HistoryTab } from './HistoryTab'
import * as shopApi from './api'
import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import { i18next } from '../../i18n/all'
import zh from '../../i18n/locales/zh-CN.json'

// Every Personal Shopper text field has an accessible name. A placeholder is
// not one: it vanishes once text is typed, and assistive tech may skip it.

vi.mock('./api', () => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() }))

const mockGet = vi.mocked(shopApi.get)
const zhShop = zh.apps.personalShopper

beforeEach(() => {
  vi.clearAllMocks()
  mockGet.mockImplementation(async (path: string) => {
    if (path === '/preferences') return { preferences: [] } as never
    if (path === '/groups') return { groups: [] } as never
    if (path === '/sites') return { sites: [] } as never
    return { sessions: [] } as never
  })
})
afterEach(async () => { await i18next.changeLanguage('en') })

describe('Personal Shopper field names', () => {
  it('names the new preference and group name fields', async () => {
    renderWithProviders(<PreferencesTab />)
    expect(await screen.findByRole('textbox', { name: 'New preference' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: i18nT('apps.personalShopper.preferencesTab.add_group') }))
    expect(screen.getByRole('textbox', { name: 'Group name' })).toBeInTheDocument()
  })

  it('names the site name and URL fields', async () => {
    renderWithProviders(<SitesTab />)
    fireEvent.click(await screen.findByText(i18nT('apps.personalShopper.sitesTab.add_shopping_site')))
    expect(screen.getByRole('textbox', { name: 'Site name' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Site URL' })).toBeInTheDocument()
  })

  it('names the history search field', async () => {
    renderWithProviders(<HistoryTab />)
    expect(await screen.findByRole('textbox', { name: 'Search past sessions' })).toBeInTheDocument()
  })

  it('takes the names from the catalog in another language', async () => {
    await i18next.changeLanguage('zh-CN')
    renderWithProviders(<HistoryTab />)
    expect(await screen.findByRole('textbox', { name: zhShop.historyTab.search_label })).toBeInTheDocument()
  })
})
