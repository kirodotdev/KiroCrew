import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, cleanup } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import type { FileContents } from '@pierre/diffs'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact } from '../types'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))
// The diff is drawn by Pierre inside a shadow root (see DiffPanel.cov80.test.tsx), so what this
// page owns is the pair of texts it hands over, and which one is on the older side.
const pair: { oldFile: FileContents | null; newFile: FileContents | null; diffStyle: string | undefined } = {
  oldFile: null, newFile: null, diffStyle: undefined,
}
const viewport = { mobile: false }
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => viewport.mobile }))
vi.mock('../pierre', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../pierre')>()),
  PierreFilePair: (props: { oldFile: FileContents | null; newFile: FileContents | null; options?: { diffStyle?: string } }) => {
    pair.oldFile = props.oldFile
    pair.newFile = props.newFile
    pair.diffStyle = props.options?.diffStyle
    return <div data-testid="pierre-pair" />
  },
}))

const mkArtifact = (overrides: Partial<Artifact> = {}): Artifact => ({
  slug: 'plan',
  name: 'Plan',
  kind: 'markdown',
  source: 'chat',
  description: '',
  tags: [],
  version: 3,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: '# Plan\n\nlive text',
  ...overrides,
})

const SNAPSHOTS: Record<number, string> = { 1: '# Plan\n\nfirst text', 2: '# Plan\n\nsecond text', 3: '# Plan\n\nthird text' }

function renderRoute() {
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
    </Routes>,
    { route: '/artifacts/plan' },
  )
}

const compareTrigger = () => screen.getByRole('combobox', { name: /Compare with/i })
const queryCompareTrigger = () => screen.queryByRole('combobox', { name: /Compare with/i })

async function pick(trigger: HTMLElement, label: string) {
  fireEvent.click(trigger)
  fireEvent.click(await screen.findByRole('option', { name: label }))
}

describe('ArtifactDetailPage — compare with another version', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    pair.oldFile = null
    pair.newFile = null
    pair.diffStyle = undefined
    viewport.mobile = false
    localStorage.removeItem('mc-diff-split')
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'plan', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'plan', versions: [1, 2, 3] })
    vi.mocked(api).artifactVersion = vi.fn((_slug: string, n: number) =>
      Promise.resolve(mkArtifact({ version: n, content: SNAPSHOTS[n] })),
    ) as typeof api.artifactVersion
  })

  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.removeItem('mc-diff-split')
  })

  it('shows the plain document and fetches no snapshot until a comparison is asked for', async () => {
    renderRoute()
    await waitFor(() => expect(screen.getByRole('combobox', { name: /^Version$/i })).toBeInTheDocument())
    expect(screen.queryByTestId('artifact-version-compare')).toBeNull()
    expect(api.artifactVersion).not.toHaveBeenCalled()
  })

  it('offers each other state, newest snapshot first', async () => {
    renderRoute()
    await waitFor(() => expect(compareTrigger()).toBeInTheDocument())
    fireEvent.click(compareTrigger())
    const rows = (await screen.findAllByRole('option')).map((o) => o.textContent?.trim())
    expect(rows).toEqual(['Compare with', 'Compare with v3', 'Compare with v2', 'Compare with v1'])
  })

  it('puts the older snapshot on the left and Live on the right', async () => {
    renderRoute()
    await waitFor(() => expect(compareTrigger()).toBeInTheDocument())
    await pick(compareTrigger(), 'Compare with v1')
    await waitFor(() => expect(pair.oldFile?.contents).toBe(SNAPSHOTS[1]))
    expect(pair.newFile?.contents).toBe('# Plan\n\nlive text')
    expect(api.artifactVersion).toHaveBeenCalledWith('plan', 1)
    expect(screen.getByText('Comparing v1 with Live')).toBeInTheDocument()
  })

  it('draws two columns on a wide screen and one unified column on a narrow one', async () => {
    renderRoute()
    await waitFor(() => expect(compareTrigger()).toBeInTheDocument())
    await pick(compareTrigger(), 'Compare with v1')
    await waitFor(() => expect(pair.diffStyle).toBe('split'))
    viewport.mobile = true
    cleanup()
    renderRoute()
    await waitFor(() => expect(compareTrigger()).toBeInTheDocument())
    await pick(compareTrigger(), 'Compare with v1')
    await waitFor(() => expect(pair.diffStyle).toBe('unified'))
  })

  it('follows the shared diff layout preference from Display settings', async () => {
    localStorage.setItem('mc-diff-split', '0')
    renderRoute()
    await waitFor(() => expect(compareTrigger()).toBeInTheDocument())
    await pick(compareTrigger(), 'Compare with v1')
    await waitFor(() => expect(pair.diffStyle).toBe('unified'))
    expect(screen.queryByTestId('artifact-version-compare-captions')).toBeNull()
    cleanup()
    localStorage.setItem('mc-diff-split', '1')
    renderRoute()
    await waitFor(() => expect(compareTrigger()).toBeInTheDocument())
    await pick(compareTrigger(), 'Compare with v1')
    await waitFor(() => expect(pair.diffStyle).toBe('split'))
    expect(screen.getByTestId('artifact-version-compare-captions')).toBeInTheDocument()
  })

  it('names the older state over the left column and the newer over the right in two-column mode', async () => {
    renderRoute()
    await waitFor(() => expect(compareTrigger()).toBeInTheDocument())
    await pick(compareTrigger(), 'Compare with v1')
    const captions = await screen.findByTestId('artifact-version-compare-captions')
    expect(Array.from(captions.children).map((c) => c.textContent)).toEqual(['v1', 'Live'])
    viewport.mobile = true
    cleanup()
    renderRoute()
    await waitFor(() => expect(compareTrigger()).toBeInTheDocument())
    await pick(compareTrigger(), 'Compare with v1')
    await screen.findByTestId('artifact-version-compare')
    expect(screen.queryByTestId('artifact-version-compare-captions')).toBeNull()
  })

  it('keeps the older side on the left when the viewed snapshot is the older one', async () => {
    renderRoute()
    await waitFor(() => expect(compareTrigger()).toBeInTheDocument())
    fireEvent.click(screen.getByRole('combobox', { name: /^Version$/i }))
    fireEvent.click(await screen.findByRole('option', { name: 'v2' }))
    await waitFor(() => expect(screen.getByText(/Showing v2/)).toBeInTheDocument())
    await pick(compareTrigger(), 'Compare with Live')
    await waitFor(() => expect(pair.newFile?.contents).toBe('# Plan\n\nlive text'))
    expect(pair.oldFile?.contents).toBe(SNAPSHOTS[2])
    expect(screen.getByText('Comparing v2 with Live')).toBeInTheDocument()
  })

  it('returns to the document when the comparison is stopped', async () => {
    renderRoute()
    await waitFor(() => expect(compareTrigger()).toBeInTheDocument())
    await pick(compareTrigger(), 'Compare with v2')
    await screen.findByTestId('artifact-version-compare')
    fireEvent.click(screen.getByRole('button', { name: /Stop comparing/i }))
    await waitFor(() => expect(screen.queryByTestId('artifact-version-compare')).toBeNull())
  })

  it('drops the comparison when the viewed state becomes the compared one', async () => {
    renderRoute()
    await waitFor(() => expect(compareTrigger()).toBeInTheDocument())
    await pick(compareTrigger(), 'Compare with v2')
    await screen.findByTestId('artifact-version-compare')
    fireEvent.click(screen.getByRole('combobox', { name: /^Version$/i }))
    fireEvent.click(await screen.findByRole('option', { name: 'v2' }))
    await waitFor(() => expect(screen.queryByTestId('artifact-version-compare')).toBeNull())
  })

  it('does not bring a dropped comparison back when the viewed state returns to Live', async () => {
    renderRoute()
    await waitFor(() => expect(compareTrigger()).toBeInTheDocument())
    await pick(compareTrigger(), 'Compare with v2')
    await screen.findByTestId('artifact-version-compare')
    await pick(screen.getByRole('combobox', { name: /^Version$/i }), 'v2')
    await waitFor(() => expect(screen.queryByTestId('artifact-version-compare')).toBeNull())
    await pick(screen.getByRole('combobox', { name: /^Version$/i }), 'Live')
    await waitFor(() => expect(screen.queryByText(/Showing v2/)).toBeNull())
    await waitFor(() => expect(compareTrigger()).toHaveTextContent(/^Compare with$/))
    expect(screen.queryByTestId('artifact-version-compare')).toBeNull()
  })

  it('does not bring a comparison back after an edit starts and ends', async () => {
    renderRoute()
    await waitFor(() => expect(compareTrigger()).toBeInTheDocument())
    await pick(compareTrigger(), 'Compare with v2')
    await screen.findByTestId('artifact-version-compare')
    fireEvent.click(screen.getByRole('button', { name: 'Edit content' }))
    await waitFor(() => expect(screen.queryByTestId('artifact-version-compare')).toBeNull())
    fireEvent.click(await screen.findByRole('button', { name: /^Cancel$/ }))
    await waitFor(() => expect(compareTrigger()).toBeInTheDocument())
    expect(compareTrigger()).toHaveTextContent(/^Compare with$/)
    expect(screen.queryByTestId('artifact-version-compare')).toBeNull()
  })

  it('offers no comparison for an artifact with no other state or no text', async () => {
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'plan', versions: [] })
    renderRoute()
    await waitFor(() => expect(screen.getByRole('combobox', { name: /^Version$/i })).toBeInTheDocument())
    expect(queryCompareTrigger()).toBeNull()
  })

  it('offers no comparison for an image', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'image', content: undefined }))
    renderRoute()
    await waitFor(() => expect(screen.getByRole('combobox', { name: /^Version$/i })).toBeInTheDocument())
    expect(queryCompareTrigger()).toBeNull()
  })

  it('says so, and keeps the page, when the other snapshot cannot be read', async () => {
    vi.mocked(api).artifactVersion = vi.fn().mockRejectedValue(new Error('snapshot gone'))
    renderRoute()
    await waitFor(() => expect(compareTrigger()).toBeInTheDocument())
    await pick(compareTrigger(), 'Compare with v1')
    expect(await screen.findByText('Could not load v1 to compare')).toBeInTheDocument()
    expect(screen.getByText(/snapshot gone/)).toBeInTheDocument()
    expect(screen.getByText('Plan')).toBeInTheDocument()
  })
})
