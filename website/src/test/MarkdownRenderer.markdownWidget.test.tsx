/**
 * A `<mcwidget>` whose body is markdown, not HTML, renders as markdown under its
 * title instead of raw `**` and `|` text in a frame. An HTML body still goes to
 * the widget frame.
 *
 * The title row keeps the artifact identity every widget has: re-emitted with a
 * `slug`, the markdown-bodied widget links its title to the artifact page and
 * shows the star, the same header the HTML frame draws (#25102).
 */
import { describe, it, expect, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: true }),
}))
vi.mock('../components/WidgetFrame', () => ({
  default: ({ title }: { title?: string }) => <div data-testid="widget-frame">{title}</div>,
}))
vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error { status = 0 },
  api: {
    artifact: vi.fn().mockResolvedValue({ slug: 'task-board', pinned: true }),
    createArtifact: vi.fn(),
    setArtifactPinned: vi.fn(),
  },
}))

import MarkdownRenderer from '../components/MarkdownRenderer'

const MD_BODY = [
  '',
  '**Needs you:** Nothing right now.',
  '',
  '| Task | State |',
  '|---|---|',
  '| Welcome note | working |',
  '',
].join('\n')

const MD_WIDGET = ['Here is where things stand.', '', '<mcwidget title="Task board">', MD_BODY, '</mcwidget>'].join('\n')

function renderWithQuery(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('markdown inside a widget tag', () => {
  it('renders the table as a table and the bold as bold, under the title', () => {
    const { getByTestId, container, queryByTestId } = renderWithQuery(<MarkdownRenderer content={MD_WIDGET} />)
    const card = getByTestId('markdown-widget')
    expect(card).toHaveAttribute('aria-label', 'Task board')
    expect(card.querySelector('table')).not.toBeNull()
    expect(card.querySelector('strong')?.textContent).toBe('Needs you:')
    expect(container.textContent).not.toContain('|---|')
    expect(queryByTestId('widget-frame')).toBeNull()
  })

  it('re-emitted with a slug, links its title to the artifact and shows the star', async () => {
    const withSlug = ['<mcwidget title="Task board" slug="task-board">', MD_BODY, '</mcwidget>'].join('\n')
    const { getByTestId, getByRole } = renderWithQuery(<MarkdownRenderer content={withSlug} messageTs="1700000000.1" slotKey="chat-1-1" />)
    const card = getByTestId('markdown-widget')
    await waitFor(() => {
      const link = card.querySelector('a[href="/artifacts/task-board"]')
      expect(link?.textContent).toBe('Task board')
    })
    expect(getByRole('button', { name: /remove artifact|starred|star/i })).toBeInTheDocument()
  })

  it('a failed artifact read says so instead of looking like no artifact', async () => {
    const { api } = await import('../api/client')
    const broke = Object.assign(new Error('zzq-probe-broke'), { status: 500 })
    vi.mocked(api.artifact).mockRejectedValueOnce(broke as never)
    const { getByTestId, findByRole, queryByRole } = renderWithQuery(<MarkdownRenderer content={MD_WIDGET} messageTs="1700000000.2" slotKey="chat-1-1" />)
    getByTestId('markdown-widget')
    expect(await findByRole('alert')).toHaveTextContent('zzq-probe-broke')
    expect(queryByRole('link')).toBeNull()
  })

  it('a 404 is the benign no-artifact state: no notice, no link, a hollow star', async () => {
    const { api, ApiError } = await import('../api/client')
    const missing = Object.assign(new ApiError('zzq-missing'), { status: 404 })
    vi.mocked(api.artifact).mockRejectedValueOnce(missing as never)
    const { getByTestId, findByRole, queryByRole } = renderWithQuery(<MarkdownRenderer content={MD_WIDGET} messageTs="1700000000.3" slotKey="chat-1-1" />)
    getByTestId('markdown-widget')
    const star = await findByRole('button', { name: /star/i })
    await waitFor(() => expect(vi.mocked(api.artifact)).toHaveBeenCalled())
    expect(queryByRole('alert')).toBeNull()
    expect(queryByRole('link')).toBeNull()
    expect(star).toBeEnabled()
  })

  it('an HTML body still goes to the widget frame', () => {
    const html = '<mcwidget title="Card">\n<div style="color:red">Hi</div>\n</mcwidget>'
    const { getByTestId, queryByTestId } = renderWithQuery(<MarkdownRenderer content={html} />)
    expect(getByTestId('widget-frame')).toHaveTextContent('Card')
    expect(queryByTestId('markdown-widget')).toBeNull()
  })
})
