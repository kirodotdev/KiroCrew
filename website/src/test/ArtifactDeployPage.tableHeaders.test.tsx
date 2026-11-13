/**
 * The AWS Profiles and Deployments table headers follow the active language.
 *
 * Both header rows were plain English arrays mapped straight into `<th>`, so
 * every locale rendered "Name / Account / Region / ..." while the rest of the
 * page was translated. This mounts the page under Japanese with one profile and
 * one deployed site, and reads the header cells of both tables.
 */

import { describe, it, expect, vi, afterAll, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import React from 'react'

import ArtifactDeployPage from '../pages/ArtifactDeployPage'
// `/all` for the Japanese catalog: `../i18n` registers English only.
import { i18next } from '../i18n/all'

const ok = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

function installFetch() {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const u = String(input)
    if (u.startsWith('/api/artifacts')) return ok({ artifacts: [] })
    if (u.endsWith('/list')) {
      return ok({
        configured: true,
        sites: [{ site_id: 'shopfront', status: 'deployed', profile: 'ship', url: 'https://d1.cloudfront.net/' }],
      })
    }
    if (u.endsWith('/pending')) return ok({ pending: [] })
    if (u.includes('/profiles')) {
      return ok({
        profiles: [{ name: 'ship', region: 'us-west-2', account: '1', verified_at: '', note: '' }],
        default: 'ship', available: [],
      })
    }
    return ok({})
  })
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><ArtifactDeployPage /></MemoryRouter>
    </QueryClientProvider>,
  )
}

/** The header cells of the table whose body holds `rowText`. */
function headersOf(rowText: string): string[] {
  const table = screen.getAllByRole('table').find((t) => t.textContent?.includes(rowText))
  if (!table) throw new Error(`no table holds ${rowText}`)
  return Array.from(table.querySelectorAll('thead th')).map((th) => th.textContent ?? '')
}

afterEach(() => { vi.restoreAllMocks() })
afterAll(async () => { await i18next.changeLanguage('en') })

describe('ArtifactDeployPage table headers follow the active language', () => {
  it('renders the AWS Profiles headers from the catalog', async () => {
    await i18next.changeLanguage('ja')
    installFetch()
    renderPage()
    await waitFor(() => expect(screen.getAllByText('us-west-2').length).toBeGreaterThan(0))
    expect(headersOf('us-west-2')).toEqual(['', '名前', 'アカウント', 'リージョン', 'ステータス', 'アクション'])
  })

  it('renders the Deployments headers from the catalog', async () => {
    await i18next.changeLanguage('ja')
    installFetch()
    renderPage()
    await waitFor(() => expect(screen.getByText('shopfront')).toBeTruthy())
    expect(headersOf('shopfront')).toEqual(['名前', 'タイプ', 'ステータス', 'プロファイル', 'リンク', 'コスト', 'アクション'])
  })

  it('keeps the English headers under English', async () => {
    await i18next.changeLanguage('en')
    installFetch()
    renderPage()
    await waitFor(() => expect(screen.getByText('shopfront')).toBeTruthy())
    expect(headersOf('shopfront')).toEqual(['Name', 'Type', 'Status', 'Profile', 'URL', 'Cost', 'Actions'])
    // The profiles table comes from its own query; wait for it too.
    await waitFor(() => expect(screen.getAllByText('us-west-2').length).toBeGreaterThan(0))
    expect(headersOf('us-west-2')).toEqual(['', 'Name', 'Account', 'Region', 'Status', 'Actions'])
  })
})
