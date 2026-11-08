/**
 * The full-screen file view's header clears the window caption band (#24784).
 *
 * The full-screen view is portaled to <body>, outside the shell div that
 * carries `.win-electron`, so the header's caption inset never reaches it. On a
 * frameless desktop window the native minimize/maximize/close buttons then sit
 * on top of Exit full screen, and only Esc closes the view. The header drops
 * below the band via overlayCaptionClearancePx(); the embedded Windows-host
 * class is the branch that helper reads at render time, so no module reload is
 * needed to reach it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreCode: ({ file }: { file: { contents: string } }) => (
    <div data-testid="pierre-code" data-value={file.contents} />
  ),
}))

vi.mock('../api/client', () => ({
  api: {
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    artifact: vi.fn().mockResolvedValue({ live_dirty: false, pinned: false }),
    fileDiff: vi.fn().mockResolvedValue({ diff: '', original: '', status: 'clean' }),
    revealPath: vi.fn(),
  },
}))

const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

beforeEach(() => {
  qc.clear()
  vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
    const url = String(input)
    if (url.startsWith('/api/knowledge/config')) return { ok: true, json: async () => ({ enabled: false, supported_formats: [] }) }
    if (url.startsWith('/api/knowledge/sources')) return { ok: true, json: async () => [] }
    return { ok: true, status: 200, headers: { get: () => null }, text: async () => '# Title\n' }
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.style.overflow = ''
  document.documentElement.classList.remove('embedded-win-inset')
})

async function fullscreenHeader(): Promise<HTMLElement> {
  render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <MarkdownPanel embedded filePath="/tmp/notes.md" content={'# Title\n'} onContentChange={vi.fn()} onSave={vi.fn(async () => {})} onClose={vi.fn()} initialDiffMode={false} />
      </QueryClientProvider>
    </MemoryRouter>,
  )
  fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[0])
  fireEvent.click(screen.getByText('Full screen'))
  await screen.findByRole('dialog')
  return screen.getByTestId('markdown-panel-fullscreen-header')
}

describe('MarkdownPanel full-screen header and the caption band (#24784)', () => {
  it('drops below the caption band under a Windows host', async () => {
    document.documentElement.classList.add('embedded-win-inset')
    expect((await fullscreenHeader()).style.marginTop).toBe('42px')
  })

  it('stays at the top in a plain browser tab', async () => {
    expect((await fullscreenHeader()).style.marginTop).toBe('')
  })
})
