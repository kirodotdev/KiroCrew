/**
 * Edition tabs on an opened file or artifact in the chat side panel: a component
 * registered with `registerDocumentPanelTabs` shows as a tab beside the document,
 * gets the document it sits on, follows its app's enable state, and a tab that
 * throws is isolated while the document stays usable.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useEffect } from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { DocumentPanelTabs } from './DocumentPanelTabs'
import {
  registerDocumentPanelTabs,
  resetDocumentPanelTabsForTests,
  type DocumentPanelTabProps,
  type DocumentPanelTarget,
} from './documentPanelTabRegistry'

const seen: DocumentPanelTabProps[] = []

function SampleTab(props: DocumentPanelTabProps) {
  seen.push(props)
  const t = props.target
  return <div>sample body for {t.kind === 'file' ? t.path : t.slug}</div>
}

function BrokenTab(): never {
  throw new Error('tab failed')
}

function mount(target: DocumentPanelTarget, apps: unknown[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['apps'], apps)
  render(
    <QueryClientProvider client={qc}>
      <DocumentPanelTabs target={target} active slot="s1">
        <div>document body</div>
      </DocumentPanelTabs>
    </QueryClientProvider>,
  )
}

const fileTarget: DocumentPanelTarget = { kind: 'file', path: '/w/notes/plan.md' }

beforeEach(() => {
  seen.length = 0
  resetDocumentPanelTabsForTests()
})

afterEach(() => {
  vi.restoreAllMocks()
  resetDocumentPanelTabsForTests()
})

describe('DocumentPanelTabs', () => {
  it('renders the document alone, with no tab strip, when nothing is registered', () => {
    mount(fileTarget)
    expect(screen.getByText('document body')).toBeInTheDocument()
    expect(screen.queryByRole('tablist')).toBeNull()
  })

  it('shows no tab while the app the tab requires is disabled', () => {
    registerDocumentPanelTabs([
      { id: 'sample', label: () => 'Sample', requiresApp: 'sample-app', component: SampleTab },
    ])
    mount(fileTarget, [{ name: 'sample-app', enabled: false }])
    expect(screen.getByText('document body')).toBeInTheDocument()
    expect(screen.queryByRole('tablist')).toBeNull()
  })

  it('shows the tab while its app is enabled and passes it the opened document', () => {
    registerDocumentPanelTabs([
      { id: 'sample', label: () => 'Sample', requiresApp: 'sample-app', component: SampleTab },
    ])
    mount(fileTarget, [{ name: 'sample-app', enabled: true }])
    expect(screen.getByRole('tab', { name: 'plan.md' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(screen.getByRole('tab', { name: 'Sample' }))
    expect(screen.getByRole('tab', { name: 'Sample' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('sample body for /w/notes/plan.md')).toBeInTheDocument()
    expect(seen.at(-1)).toMatchObject({ target: fileTarget, active: true, slot: 's1' })
    // The document stays mounted (hidden), so an unsaved buffer survives the switch.
    expect(screen.getByText('document body')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'plan.md' }))
    expect(screen.queryByText('sample body for /w/notes/plan.md')).toBeNull()
  })

  it('names an artifact by its slug', () => {
    registerDocumentPanelTabs([{ id: 'sample', label: () => 'Sample', component: SampleTab }])
    mount({ kind: 'artifact', slug: 'weekly-report', artifactKind: 'markdown' })
    fireEvent.click(screen.getByRole('tab', { name: 'Sample' }))
    expect(screen.getByText('sample body for weekly-report')).toBeInTheDocument()
  })

  it('shows the crash card for a tab that throws and keeps the document', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    registerDocumentPanelTabs([{ id: 'broken', label: () => 'Broken', component: BrokenTab }])
    mount(fileTarget)
    fireEvent.click(screen.getByRole('tab', { name: 'Broken' }))
    expect(screen.getByText('tab failed')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'plan.md' }))
    expect(screen.getByText('document body')).toBeVisible()
  })

  it('treats a duplicate id as a seam collision', () => {
    registerDocumentPanelTabs([{ id: 'sample', label: () => 'Sample', component: SampleTab }])
    expect(() =>
      registerDocumentPanelTabs([{ id: 'sample', label: () => 'Again', component: SampleTab }]),
    ).toThrow(/sample/)
  })

  it('tells the hidden document it is not active while an edition tab shows', () => {
    // Opus/GPT review: a hidden document with active=true still takes Cmd+S and Escape.
    registerDocumentPanelTabs([{ id: 'sample', label: () => 'Sample', component: SampleTab }])
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['apps'], [])
    const seenActive: boolean[] = []
    render(
      <QueryClientProvider client={qc}>
        <DocumentPanelTabs target={fileTarget} active slot="s1">
          {(docActive: boolean) => { seenActive.push(docActive); return <div>document body</div> }}
        </DocumentPanelTabs>
      </QueryClientProvider>,
    )
    expect(seenActive.at(-1)).toBe(true)
    fireEvent.click(screen.getByRole('tab', { name: 'Sample' }))
    expect(seenActive.at(-1)).toBe(false)
    fireEvent.click(screen.getByRole('tab', { name: 'plan.md' }))
    expect(seenActive.at(-1)).toBe(true)
  })

  it('keeps the document mounted when an app-dependent tab appears', async () => {
    // GPT review F2: the strip appearing must not remount an open editor.
    registerDocumentPanelTabs([
      { id: 'sample', label: () => 'Sample', requiresApp: 'sample-app', component: SampleTab },
    ])
    let mounts = 0
    function Doc() {
      useEffect(() => { mounts += 1 }, [])
      return <div>document body</div>
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['apps'], [])
    render(
      <QueryClientProvider client={qc}>
        <DocumentPanelTabs target={fileTarget} active slot="s1"><Doc /></DocumentPanelTabs>
      </QueryClientProvider>,
    )
    expect(screen.queryByRole('tablist')).toBeNull()
    act(() => { qc.setQueryData(['apps'], [{ name: 'sample-app', enabled: true }]) })
    expect(await screen.findByRole('tab', { name: 'Sample' })).toBeInTheDocument()
    expect(mounts).toBe(1)
  })

  it('offers no hard navigation from a crashed tab, so the hidden buffer survives', () => {
    // GPT review: "Ask the agent" hard-navigates and drops the document's unsaved edits.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    registerDocumentPanelTabs([{ id: 'broken', label: () => 'Broken', component: BrokenTab }])
    mount(fileTarget)
    fireEvent.click(screen.getByRole('tab', { name: 'Broken' }))
    expect(screen.getByText('tab failed')).toBeInTheDocument()
    expect(screen.queryByText('Ask the agent')).toBeNull()
  })

  it('shows the document again when a new reveal arrives for it', () => {
    // Opus review: reopening the file at a line must not leave the edition tab on top.
    registerDocumentPanelTabs([{ id: 'sample', label: () => 'Sample', component: SampleTab }])
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['apps'], [])
    const tree = (revealKey?: number) => (
      <QueryClientProvider client={qc}>
        <DocumentPanelTabs target={fileTarget} active slot="s1" revealKey={revealKey}>
          <div>document body</div>
        </DocumentPanelTabs>
      </QueryClientProvider>
    )
    const { rerender } = render(tree())
    fireEvent.click(screen.getByRole('tab', { name: 'Sample' }))
    expect(screen.getByRole('tab', { name: 'Sample' })).toHaveAttribute('aria-selected', 'true')
    rerender(tree(7))
    expect(screen.getByRole('tab', { name: 'plan.md' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('document body')).toBeVisible()
  })

  it('is mounted around file and artifact tab bodies', () => {
    const src = readFileSync(resolve(__dirname, 'SidePanelTabBodies.tsx'), 'utf8')
    expect(src).toContain("<DocumentPanelTabs target={{ kind: 'file'")
    expect(src).toContain("<DocumentPanelTabs target={{ kind: 'artifact'")
    expect(src).toContain('revealKey={tab.revealLine?.nonce}')
  })
})
