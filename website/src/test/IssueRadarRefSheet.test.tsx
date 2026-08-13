import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// The context is mocked (same pattern as IssueRadarTagging.test.tsx) so these
// tests exercise the reference UI without standing up the whole data layer.
const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({
  useIssueRadar: () => ctx.value,
}))

const api = { refSummary: vi.fn() }
vi.mock('../apps/issue-radar/api', () => ({ issueRadarApi: api }))

// MarkdownRenderer is stubbed to a faithful miniature of the real one: it owns a
// LinkOverrideCtx and renders the content as ONE anchor, asking the override
// first — exactly what MdAnchor does. That keeps the heavy markdown pipeline
// (mermaid/Monaco/KaTeX) out of these tests while still routing the link through
// the real seam, the real parseRepoRef, and the real RefLink.
vi.mock('../components/MarkdownRenderer', async () => {
  const react = await import('react')
  const Ctx = react.createContext<((l: { href: string; children: React.ReactNode }) => React.ReactNode | null) | null>(null)
  const Renderer = ({ content }: { content: string }) => {
    const override = react.useContext(Ctx)
    const href = /\(([^)]+)\)/.exec(content)?.[1] ?? content
    const claimed = override?.({ href, children: 'the link' })
    return claimed ? <>{claimed}</> : <a href={href}>the link</a>
  }
  return { default: Renderer, LinkOverrideCtx: Ctx }
})

// The sheet's payload is the real detail pane; stub it so the tests assert on
// the sheet's own chrome and target routing.
vi.mock('../apps/issue-radar/components/IssueDetail', () => ({
  default: ({ issue }: { issue: { number: number; state?: string } }) => (
    <div>issue pane {issue.number} state={String(issue.state)}</div>
  ),
}))
vi.mock('../apps/issue-radar/components/PrDetail', () => ({
  default: ({ pull }: { pull: { number: number } }) => <div>pr pane {pull.number}</div>,
}))

const RefMarkdown = (await import('../apps/issue-radar/components/RefMarkdown')).default
const RefSheet = (await import('../apps/issue-radar/components/RefSheet')).default

const OWNER = 'kirodotdev'
const REPO = 'KiroCrew'

function summary(over: Record<string, unknown> = {}) {
  return {
    owner: OWNER, repo: REPO, number: 533, from_cache: false,
    summary: {
      number: 533, title: 'Tagging dashboard', state: 'open', state_reason: null,
      url: `https://github.com/${OWNER}/${REPO}/issues/533`, author: 'alice',
      author_association: 'MEMBER', created_at: '2026-07-01T00:00:00Z',
      updated_at: '2026-07-02T00:00:00Z', closed_at: null, comments: 2,
      is_pr: false, draft: false, merged_at: null, labels: [],
      ...over,
    },
  }
}

function setCtx(over: Record<string, unknown> = {}) {
  ctx.value = {
    active: { owner: OWNER, repo: REPO },
    refStack: [],
    openRef: vi.fn(),
    popRef: vi.fn(),
    closeRefs: vi.fn(),
    issues: [],
    pulls: [],
    setSelectedIssue: vi.fn(),
    setSelectedPull: vi.fn(),
    openIssues: vi.fn(),
    openPulls: vi.fn(),
    ...over,
  }
  return ctx.value
}

function renderWithQc(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  api.refSummary.mockReset()
  api.refSummary.mockResolvedValue(summary())
  setCtx()
})

describe('RefMarkdown link interception', () => {
  it('opens a same-repo issue link in the app instead of navigating', async () => {
    const v = setCtx()
    renderWithQc(<RefMarkdown content={`https://github.com/${OWNER}/${REPO}/issues/533`} />)
    await userEvent.click(screen.getByRole('link'))
    expect(v.openRef).toHaveBeenCalledWith({ kind: 'issue', number: 533 })
  })

  it('opens a same-repo pull-request link in the app', async () => {
    const v = setCtx()
    renderWithQc(<RefMarkdown content={`https://github.com/${OWNER}/${REPO}/pull/548`} />)
    await userEvent.click(screen.getByRole('link'))
    expect(v.openRef).toHaveBeenCalledWith({ kind: 'pull', number: 548 })
  })

  it('claims a bare #123 shorthand, which linkify turned into a link', async () => {
    const v = setCtx()
    renderWithQc(<RefMarkdown content="duplicate of #123" />)
    await userEvent.click(screen.getByRole('link'))
    expect(v.openRef).toHaveBeenCalledWith({ kind: 'issue', number: 123 })
  })

  it('marks a claimed reference with the dashed accent underline', () => {
    setCtx()
    renderWithQc(<RefMarkdown content={`https://github.com/${OWNER}/${REPO}/issues/533`} />)
    expect(screen.getByRole('link').className).toContain('decoration-dashed')
  })

  it('leaves a link to a DIFFERENT repo alone', async () => {
    const v = setCtx()
    renderWithQc(<RefMarkdown content={`https://github.com/${OWNER}/OtherRepo/issues/1`} />)
    const link = screen.getByRole('link')
    expect(link.className).not.toContain('decoration-dashed')
    await userEvent.click(link)
    expect(v.openRef).not.toHaveBeenCalled()
  })

  it('leaves a non-issue GitHub link alone', async () => {
    const v = setCtx()
    renderWithQc(<RefMarkdown content={`https://github.com/${OWNER}/${REPO}/discussions/4`} />)
    await userEvent.click(screen.getByRole('link'))
    expect(v.openRef).not.toHaveBeenCalled()
  })

  it('lets a modified click through so open-in-new-tab still works', () => {
    const v = setCtx()
    renderWithQc(<RefMarkdown content={`https://github.com/${OWNER}/${REPO}/issues/533`} />)
    const link = screen.getByRole('link')
    for (const mod of ['metaKey', 'ctrlKey', 'shiftKey', 'altKey'] as const) {
      const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0, [mod]: true })
      link.dispatchEvent(event)
      expect(event.defaultPrevented).toBe(false)
    }
    expect(v.openRef).not.toHaveBeenCalled()
  })

  it('prevents the default navigation on a claimed click', () => {
    setCtx()
    renderWithQc(<RefMarkdown content={`https://github.com/${OWNER}/${REPO}/issues/533`} />)
    const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
    screen.getByRole('link').dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
  })

  it('keeps the href and new-tab target so the link still works without JS', () => {
    setCtx()
    renderWithQc(<RefMarkdown content={`https://github.com/${OWNER}/${REPO}/issues/533`} />)
    const link = screen.getByRole('link')
    expect(link.getAttribute('href')).toBe(`https://github.com/${OWNER}/${REPO}/issues/533`)
    expect(link.getAttribute('target')).toBe('_blank')
  })
})

describe('RefLink hover preview', () => {
  it('fetches nothing until the reference is hovered', () => {
    setCtx()
    renderWithQc(<RefMarkdown content={`https://github.com/${OWNER}/${REPO}/issues/533`} />)
    expect(api.refSummary).not.toHaveBeenCalled()
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('shows number, title, author, time and state on hover', async () => {
    setCtx()
    renderWithQc(<RefMarkdown content={`https://github.com/${OWNER}/${REPO}/issues/533`} />)
    await userEvent.hover(screen.getByRole('link'))
    const card = await waitFor(() => screen.getByRole('tooltip'), { timeout: 3000 })
    await waitFor(() => expect(card.textContent).toContain('Tagging dashboard'))
    expect(api.refSummary).toHaveBeenCalledWith({ owner: OWNER, repo: REPO }, 533)
    expect(card.textContent).toContain('#533')
    expect(card.textContent).toContain('alice')
    expect(card.textContent).toContain('Open')
    expect(card.textContent).toMatch(/opened/)
  })

  it('reads a merged pull request as merged, not closed', async () => {
    api.refSummary.mockResolvedValue(summary({ is_pr: true, state: 'closed', merged_at: '2026-07-03T00:00:00Z' }))
    setCtx()
    renderWithQc(<RefMarkdown content={`https://github.com/${OWNER}/${REPO}/pull/533`} />)
    await userEvent.hover(screen.getByRole('link'))
    const card = await waitFor(() => screen.getByRole('tooltip'), { timeout: 3000 })
    await waitFor(() => expect(card.textContent).toContain('Merged'))
  })

  it('dismisses the preview when the pointer leaves', async () => {
    setCtx()
    renderWithQc(<RefMarkdown content={`https://github.com/${OWNER}/${REPO}/issues/533`} />)
    const link = screen.getByRole('link')
    await userEvent.hover(link)
    await waitFor(() => screen.getByRole('tooltip'), { timeout: 3000 })
    await userEvent.unhover(link)
    await waitFor(() => expect(screen.queryByRole('tooltip')).toBeNull())
  })
})

describe('RefSheet', () => {
  it('renders nothing while the stack is empty', () => {
    setCtx({ refStack: [] })
    const { container } = renderWithQc(<RefSheet />)
    expect(container.querySelector('[role="dialog"]')).toBeNull()
  })

  it('renders the issue pane for the top of the stack', async () => {
    setCtx({ refStack: [{ kind: 'issue', number: 533 }] })
    renderWithQc(<RefSheet />)
    expect(await screen.findByText(/issue pane 533/)).toBeTruthy()
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getByText('#533')).toBeTruthy()
  })

  it('renders the PR pane for an explicit pull target without a lookup', () => {
    setCtx({ refStack: [{ kind: 'pull', number: 548 }] })
    renderWithQc(<RefSheet />)
    expect(screen.getByText('pr pane 548')).toBeTruthy()
    expect(api.refSummary).not.toHaveBeenCalled()
  })

  it('renders the PR pane when a #number turns out to be a pull request', async () => {
    api.refSummary.mockResolvedValue(summary({ number: 630, is_pr: true }))
    setCtx({ refStack: [{ kind: 'issue', number: 630 }] })
    renderWithQc(<RefSheet />)
    expect(await screen.findByText('pr pane 630')).toBeTruthy()
    expect(screen.queryByText(/issue pane 630/)).toBeNull()
  })

  it('seeds the placeholder row with the summary state, not a guess', async () => {
    api.refSummary.mockResolvedValue(summary({ number: 9, state: 'closed' }))
    setCtx({ refStack: [{ kind: 'issue', number: 9 }], issues: [] })
    renderWithQc(<RefSheet />)
    // A guessed 'open' would let the pane offer "Close as completed" on an issue
    // that is already closed, overwriting its state_reason.
    expect(await screen.findByText(/issue pane 9 state=closed/)).toBeTruthy()
  })

  it('degrades to the issue pane when the lookup fails', async () => {
    api.refSummary.mockRejectedValue(new Error('boom'))
    setCtx({ refStack: [{ kind: 'issue', number: 7 }] })
    renderWithQc(<RefSheet />)
    expect(await screen.findByText(/issue pane 7/)).toBeTruthy()
  })

  it('shows only the innermost entry, with a back control', async () => {
    const v = setCtx({ refStack: [{ kind: 'issue', number: 1 }, { kind: 'pull', number: 2 }] })
    renderWithQc(<RefSheet />)
    expect(screen.getByText('pr pane 2')).toBeTruthy()
    expect(screen.queryByText(/issue pane 1/)).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: /back/i }))
    expect(v.popRef).toHaveBeenCalled()
  })

  it('hides the back control at the first level', () => {
    setCtx({ refStack: [{ kind: 'pull', number: 1 }] })
    renderWithQc(<RefSheet />)
    expect(screen.queryByRole('button', { name: /back/i })).toBeNull()
  })

  it('closes on the close button', async () => {
    const v = setCtx({ refStack: [{ kind: 'pull', number: 1 }] })
    renderWithQc(<RefSheet />)
    await userEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(v.closeRefs).toHaveBeenCalled()
  })

  it('steps back on Escape', async () => {
    const v = setCtx({ refStack: [{ kind: 'pull', number: 1 }] })
    renderWithQc(<RefSheet />)
    await userEvent.keyboard('{Escape}')
    expect(v.popRef).toHaveBeenCalled()
  })

  it('links out to GitHub for the current target', () => {
    setCtx({ refStack: [{ kind: 'pull', number: 7 }] })
    renderWithQc(<RefSheet />)
    const link = screen.getByRole('link', { name: 'Open on GitHub' })
    expect(link.getAttribute('href')).toBe(`https://github.com/${OWNER}/${REPO}/pull/7`)
  })

  it('offers "open in the workspace" only when the list holds the row', async () => {
    setCtx({ refStack: [{ kind: 'pull', number: 3 }], pulls: [] })
    const { unmount } = renderWithQc(<RefSheet />)
    expect(screen.queryByRole('button', { name: /open this item in the workspace/i })).toBeNull()
    unmount()

    const v = setCtx({
      refStack: [{ kind: 'pull', number: 3 }],
      pulls: [{ number: 3, title: 'listed', url: 'u', state: 'open', draft: false, labels: [], updated_at: '' }],
    })
    renderWithQc(<RefSheet />)
    await userEvent.click(screen.getByRole('button', { name: /open this item in the workspace/i }))
    expect(v.setSelectedPull).toHaveBeenCalledWith(3)
    expect(v.openPulls).toHaveBeenCalled()
    expect(v.closeRefs).toHaveBeenCalled()
  })
})
