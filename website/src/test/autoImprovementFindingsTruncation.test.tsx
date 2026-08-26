/**
 * The findings list is capped, and the cap has to be VISIBLE.
 *
 * The list rendered `findings.slice(0, 40)` directly beneath a stat card showing
 * `findings.length`. With 41+ findings the page therefore displayed two numbers that
 * disagree — "Findings 57" over a list of 40 — with nothing saying the list was cut.
 * A reader who counts the rows concludes the ledger lost 17 findings; a reader who
 * trusts the stat goes looking for rows that are not on screen. Raised by the UX
 * review.
 *
 * These assertions go through a real render rather than reading the source, because the
 * defect is a relationship between two rendered numbers. A source-text assertion on
 * `slice(0, FINDINGS_SHOWN)` would still pass if the notice were deleted.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'

import { createTestStore } from './helpers'
import AutoImprovementPage from '../apps/auto-improvement/AutoImprovementPage'

/** `n` ledger rows in a status that renders but draws no per-row PR query. */
function findings(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    fp: `fp-${i}`,
    kind: 'bug',
    target: `src/mod_${i}.py::fn`,
    status: 'no_defect',
  }))
}

function mountPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={createTestStore()}>
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <AutoImprovementPage />
        </QueryClientProvider>
      </MemoryRouter>
    </Provider>,
  )
}

describe('auto-improvement findings list — the cap is disclosed', () => {
  let rows = 0

  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        const body = url.includes('/findings')
          ? { findings: findings(rows) }
          : url.includes('/ruler')
            ? { status: 'calibrated' }
            : {}
        return { ok: true, status: 200, json: async () => body } as Response
      }),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('says how many of how many when the ledger exceeds the cap', async () => {
    rows = 57
    mountPage()
    // The exact cap is the component's business; what must hold is that BOTH numbers
    // are on screen together, so the stat and the row count cannot silently disagree.
    const notice = await screen.findByText(/40.*57|57.*40/)
    expect(notice).toBeTruthy()
  })

  it('renders no more rows than the cap', async () => {
    rows = 57
    const { container } = mountPage()
    await waitFor(() => expect(container.querySelectorAll('li').length).toBeGreaterThan(0))
    expect(container.querySelectorAll('li').length).toBe(40)
  })

  it('stays silent when nothing is hidden', async () => {
    // Crying "showing 12 of 12" on every small ledger trains the reader to ignore the
    // line, which is how the real truncation notice stops being read.
    rows = 12
    const { container } = mountPage()
    await waitFor(() => expect(container.querySelectorAll('li').length).toBe(12))
    expect(screen.queryByText(/showing/i)).toBeNull()
  })
})

/**
 * A refused commit must SAY SO, and an irreversible publish must be confirmed first.
 *
 * The commit route answers a refusal with HTTP 400 + `{code, error}` — a protected branch, a
 * push-policy denial, a run already in progress. The client's `mutationFn` was
 * `.then(r => r.json())` with no `res.ok` check and no `onError`, so every one of those
 * resolved as SUCCESS: react-query ran `onSuccess`, the pulse stopped, and the operator saw
 * no change and no reason. Separately the control was an icon-only 13px glyph, at identical
 * visual weight to the harmless Discuss icon beside it, that published irreversibly with no
 * prompt. Both raised by the UX review.
 */
describe('auto-improvement commit — failures are visible, publishes are confirmed', () => {
  const FILED = [{ fp: 'fp-1', kind: 'bug', target: 'src/m.py::f', status: 'filed' }]

  function mountFiled(commitResponse: { status: number; body: unknown }) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (init?.method === 'POST' && url.includes('/commit')) {
          return {
            ok: commitResponse.status < 400,
            status: commitResponse.status,
            json: async () => commitResponse.body,
          } as Response
        }
        const body = url.includes('/findings')
          ? { findings: FILED }
          : url.includes('/ruler')
            ? { status: 'calibrated' }
            : { branch: 'origin/main' }
        return { ok: true, status: 200, json: async () => body } as Response
      }),
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <Provider store={createTestStore()}>
        <MemoryRouter>
          <QueryClientProvider client={qc}>
            <AutoImprovementPage />
          </QueryClientProvider>
        </MemoryRouter>
      </Provider>,
    )
  }

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('asks for confirmation before publishing, and does nothing when declined', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { container } = mountFiled({ status: 200, body: { ok: true } })
    const btn = await screen.findByTitle(/commit/i)
    btn.click()
    expect(confirmSpy).toHaveBeenCalled()
    // Declining must not POST. The fetch mock records every call, so a publish would show up.
    const posts = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
      .filter((c) => (c[1] as RequestInit | undefined)?.method === 'POST')
    expect(posts.length, 'a declined confirmation still published').toBe(0)
    expect(container).toBeTruthy()
  })

  it('renders the backend reason when the commit is refused', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mountFiled({
      status: 400,
      body: { code: 'request_failed', error: 'branch is protected by push policy' },
    })
    const btn = await screen.findByTitle(/commit/i)
    btn.click()
    // The whole point: the operator learns WHY. Before the fix this rendered nothing at all.
    expect(await screen.findByText(/branch is protected by push policy/)).toBeTruthy()
  })
})
