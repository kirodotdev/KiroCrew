/**
 * Each label on the artifact page and the value it names come from ONE catalog
 * string, so a language can put the value where its grammar wants it.
 *
 * A label rendered as its own string with the raw value glued after it reads
 * "作成 2026-05-21T22:00:00.000000+00:00" in Japanese, where the natural order
 * puts the date first, and shows an ISO timestamp instead of a formatted date.
 * The footer dates, the timeline's actor and version labels, and the version
 * picker rows are each checked in English and in Japanese.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import { i18next } from '../i18n/all'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { __resetFormatterCache, fmtDateTime } from '../i18n/format'
import type { Artifact } from '../types'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))

const CREATED = '2026-05-21T22:00:00.000000+00:00'
const UPDATED = '2026-05-21T22:30:00.000000+00:00'

const mkArtifact = (): Artifact => ({
  slug: 'cr-queue',
  name: 'CR Queue',
  kind: 'markdown',
  source: 'chat',
  description: 'Hourly CR snapshot',
  tags: [],
  version: 3,
  created_at: CREATED,
  updated_at: UPDATED,
  content: '# Doc body',
})

function renderRoute() {
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
    </Routes>,
    { route: '/artifacts/cr-queue' },
  )
}

async function mount() {
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
  vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1, 2, 3] })
  vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({
    slug: 'cr-queue',
    events: [
      { ts: '2026-05-25T22:00:00.000Z', type: 'created', by: 'agent', version: 1 },
      { ts: '2026-05-25T22:45:00.000Z', type: 'reverted', by: 'user', version: 3, from_version: 1 },
    ],
  })
  renderRoute()
  await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
}

/** The footer line: the element that holds the created date. */
async function footerText(): Promise<string> {
  const created = fmtDateTime(CREATED)
  const node = await screen.findByText((_, el) =>
    !!el && el.tagName === 'DIV' && (el.textContent ?? '').includes(created)
      && ![...el.children].some((c) => (c.textContent ?? '').includes(created)),
  )
  return node.textContent ?? ''
}

async function versionRowLabels(): Promise<string[]> {
  fireEvent.click(screen.getByRole('combobox', { name: /Version/i }))
  // Wait for the oldest row, so the list is read after the versions query lands.
  await screen.findByRole('option', { name: 'v1' })
  return screen.getAllByRole('option').map((o) => o.textContent?.trim() ?? '')
}

describe('artifact page labels carry their values', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.sandboxDocUrl).mockResolvedValue({ url: '/sandbox-doc/test/tok' })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).listSlots = vi.fn().mockResolvedValue([])
  })

  afterEach(async () => {
    await i18next.changeLanguage('en')
    __resetFormatterCache()
  })

  it('shows formatted footer dates inside one English phrase each', async () => {
    await mount()
    const footer = await footerText()
    expect(footer).toContain(`Created ${fmtDateTime(CREATED)}`)
    expect(footer).toContain(`Updated ${fmtDateTime(UPDATED)}`)
    expect(footer).not.toContain(CREATED)
  })

  it('puts the date where Japanese wants it in the footer', async () => {
    await i18next.changeLanguage('ja')
    __resetFormatterCache()
    await mount()
    const footer = await footerText()
    expect(footer).toContain(`${fmtDateTime(CREATED)}に作成`)
    expect(footer).toContain(`${fmtDateTime(UPDATED)}に更新`)
  })

  it('renders the timeline action and actor as one English phrase', async () => {
    await mount()
    const action = await screen.findByText('Created')
    const row = action.parentElement
    expect(row?.textContent).toBe('Created by agent')
    // Only the action carries the emphasis; the actor stays muted.
    expect(action.className).toContain('font-medium')
    expect(row?.className).toContain('text-muted')
  })

  it('renders the timeline actor and versions as one phrase', async () => {
    await i18next.changeLanguage('ja')
    __resetFormatterCache()
    await mount()
    const action = await screen.findByText('作成')
    expect(action.parentElement?.textContent).toBe('作成（agent）')
    expect(screen.getByText('v1 → v3')).toBeInTheDocument()
    expect(screen.getByText('コンテンツは v1 からコピー')).toBeInTheDocument()
  })

  it('labels each version picker row from one string', async () => {
    await mount()
    expect(await versionRowLabels()).toEqual(['Live', 'v3', 'v2', 'v1'])
  })
})
