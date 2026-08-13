import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Mock the heavy ChatEmbed (pulls in the full chat renderer) — the empty-state
// path under test never mounts it, but the lazy import graph should stay light.
vi.mock('../app-sdk/ChatEmbed', () => ({ default: () => <div data-testid="chat-embed" /> }))

import SpecBuilderPage from '../apps/spec-builder/SpecBuilderPage'
import NewSpecView from '../apps/spec-builder/components/NewSpecView'
import { slugify } from '../apps/spec-builder/api'

let queryClient: QueryClient

function renderPage() {
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <SpecBuilderPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  localStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('SpecBuilderPage', () => {
  it('renders the first-run empty state when there are no specs', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: () => Promise.resolve(JSON.stringify({ specs: [] })),
    }))

    renderPage()

    await waitFor(() => {
      expect(screen.getByText('Plan your next feature with a spec')).toBeInTheDocument()
    })
    expect(screen.getByText('Start your first spec')).toBeInTheDocument()
  })

  it('shows an error banner when the specs list request fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: () => Promise.resolve({ error: 'boom' }),
    }))

    renderPage()

    await waitFor(() => {
      expect(screen.getByText('boom')).toBeInTheDocument()
    })
  })

  it('announces the error banner to assistive tech and labels its dismiss control', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: () => Promise.resolve({ error: 'boom' }),
    }))

    renderPage()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveAttribute('aria-live', 'assertive')
    // Icon-only dismiss must carry an accessible name, not just a tooltip.
    expect(screen.getByRole('button', { name: 'Dismiss error' })).toBeInTheDocument()
  })
})

describe('SpecBuilder loading pattern (Issue Radar parity)', () => {
  it('shows a skeleton with an announced status while the first fetch is pending', async () => {
    // A never-resolving fetch keeps the page in its first-load state.
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => new Promise(() => {})))
    renderPage()

    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('Loading specs…')
    // The empty state must NOT flash before the list resolves — that flash is
    // what the skeleton exists to prevent.
    expect(screen.queryByText('Plan your next feature with a spec')).toBeNull()
  })

  it('replaces the skeleton with the empty state once the list resolves empty', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: () => Promise.resolve(JSON.stringify({ specs: [] })),
    }))
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('Plan your next feature with a spec')).toBeInTheDocument()
    })
    expect(screen.queryByRole('status')).toBeNull()
  })
})

describe('SpecBuilder accessibility contract', () => {  const SPECS = [{ name: 'my-spec', phase: 'requirements', status: 'idle', running: false }]

  function stubSpecs() {
    vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string) => {
      const body = String(url).includes('/specs/')
        ? { name: 'my-spec', phase: 'requirements', status: 'idle', running: false, working_dir: '/tmp/p', files: {} }
        : { specs: SPECS }
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(body)) })
    }))
  }

  it('exposes every icon-only control with an accessible name', async () => {
    stubSpecs()
    renderPage()

    await waitFor(() => expect(screen.getByText('my-spec')).toBeInTheDocument())

    // No button may reach the DOM without a discernible name — this is the
    // regression that the icon-button audit found across the whole app.
    for (const btn of screen.getAllByRole('button')) {
      const name = btn.getAttribute('aria-label') || btn.textContent?.trim() || ''
      expect(name.length, 'button without accessible name: ' + btn.outerHTML.slice(0, 120)).toBeGreaterThan(0)
    }
  })

  it('makes spec rows keyboard-operable rather than click-only', async () => {
    stubSpecs()
    renderPage()

    const row = await screen.findByRole('button', { name: /my-spec/ })
    // Clickable gives role=button + tabIndex — a bare clickable div would have
    // neither, which is what the audit flagged.
    expect(row).toHaveAttribute('tabindex', '0')
  })

  it('gives the resize splitter value semantics and keyboard operation', async () => {
    stubSpecs()
    renderPage()

    const row = await screen.findByRole('button', { name: /my-spec/ })
    row.click()

    const splitter = await screen.findByRole('separator', { name: 'Resize document panel' })
    expect(splitter).toHaveAttribute('aria-orientation', 'vertical')
    expect(splitter).toHaveAttribute('tabindex', '0')
    // Value semantics let a screen reader announce the current split.
    expect(Number(splitter.getAttribute('aria-valuenow'))).toBeGreaterThanOrEqual(25)
    expect(Number(splitter.getAttribute('aria-valuemax'))).toBe(75)
  })
})

// The backend's spec-name rule (routes.py `_NAME_RE`) — every derived name,
// fallback included, must satisfy it or create() 400s.
const BACKEND_NAME_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/

describe('slugify — Latin input is unchanged byte-for-byte (issue #3002)', () => {
  it.each([
    ['Add login with Google so users need no passwords', 'add-login-with-google-so'],
    ['Fix the bug', 'fix-the-bug'],
    ['Add OAuth2 login!', 'add-oauth2-login'],
    ['  spaced   out  words  ', 'spaced-out-words'],
    ['snake_case stays', 'snake_case-stays'],
  ])('slugify(%j) === %j', (input, expected) => {
    expect(slugify(input)).toBe(expected)
    // Every derived name must satisfy the backend's rule — the invariant the
    // readiness change leans on now that the name cannot veto submission.
    expect(slugify(input)).toMatch(BACKEND_NAME_RE)
  })

  it('strips a leading bullet/underscore so the name starts alphanumeric', () => {
    expect(slugify('- add login')).toBe('add-login')
    expect(slugify('_cleanup the parser')).toBe('cleanup-the-parser')
    expect(slugify('- add login')).toMatch(BACKEND_NAME_RE)
  })
})

describe('slugify — non-Latin input yields a usable fallback name (issue #3002)', () => {
  const FALLBACK_RE = /^spec-[0-9a-f]{8}$/
  it.each([
    ['Korean', '한국어로만 쓴 설명입니다'],
    ['Japanese', 'ログイン機能を追加する'],
    ['Chinese', '添加登录功能'],
    ['Cyrillic', 'добавить вход в систему'],
    ['Greek', 'προσθήκη σύνδεσης'],
    ['emoji-only', '🚀🔥✨'],
    ['punctuation-only', '!!!???…'],
    ['empty', ''],
    ['whitespace-only', '   \t  '],
  ])('%s input derives a stable spec-<hash> name', (_label, input) => {
    const name = slugify(input)
    expect(name).toMatch(FALLBACK_RE)
    expect(name).toMatch(BACKEND_NAME_RE)
    // Deterministic: the branch preview and the created spec must agree.
    expect(slugify(input)).toBe(name)
  })

  it('keeps the Latin words of a mixed Korean+English description', () => {
    expect(slugify('로그인 add login 기능')).toBe('add-login')
  })

  it('derives distinct names for distinct non-Latin descriptions', () => {
    expect(slugify('한국어 설명')).not.toBe(slugify('ログインを追加'))
  })

  it('keeps the collision-retry shape valid, including at the 48-char cap', () => {
    // Mirrors NewSpecView's retry: autoName.slice(0, 44) + '-' + (Date.now() % 1000)
    const fallbackAlt = slugify('한국어 설명').slice(0, 44) + '-' + 999
    expect(fallbackAlt).toMatch(BACKEND_NAME_RE)
    // A slug already at the cap must still end up DIFFERENT after the suffix.
    const capped = slugify('implement authentication authorization credential management systems')
    expect(capped.length).toBe(48)
    const cappedAlt = capped.slice(0, 44) + '-' + 999
    expect(cappedAlt).not.toBe(capped)
    expect(cappedAlt.length).toBeLessThanOrEqual(48)
    expect(cappedAlt).toMatch(BACKEND_NAME_RE)
  })
})

describe('NewSpecView — non-Latin description enables creation (issue #3002)', () => {
  function renderNewSpec(created: Array<Record<string, unknown>>, onCreated: (n: string) => void) {
    vi.stubGlobal('fetch', vi.fn().mockImplementation((url: unknown, init?: RequestInit) => {
      const u = String(url)
      if (init?.method === 'POST' && u.includes('/specs')) {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>
        created.push(body)
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ name: body.name })) })
      }
      // /browse — both the picker's listing and the is-git probe.
      return Promise.resolve({
        ok: true, status: 200,
        text: () => Promise.resolve(JSON.stringify({ path: '/tmp/p', parent: '/', dirs: [], is_git: false })),
      })
    }))
    return render(
      <MemoryRouter>
        <QueryClientProvider client={queryClient}>
          <NewSpecView onCancel={() => {}} onCreated={onCreated} setErr={() => {}} onSettings={() => {}} />
        </QueryClientProvider>
      </MemoryRouter>,
    )
  }

  it('enables "Start the conversation" for a Korean-only description and creates with a valid name', async () => {
    const created: Array<Record<string, unknown>> = []
    const onCreated = vi.fn()
    renderNewSpec(created, onCreated)

    fireEvent.change(screen.getByLabelText('Describe what you want to do'), {
      target: { value: '한국어로만 쓴 작업 설명' },
    })
    const start = screen.getByRole('button', { name: 'Start the conversation →' })
    // Description alone is not enough — the working dir is still empty.
    expect(start).toBeDisabled()

    // Pick a working dir through the picker's manual path input.
    fireEvent.click(screen.getByRole('button', { name: 'Choose a project folder' }))
    fireEvent.click(await screen.findByText('Type a path instead'))
    fireEvent.change(screen.getByLabelText('Project folder path'), { target: { value: '/tmp/p' } })

    await waitFor(() => expect(start).not.toBeDisabled())

    fireEvent.click(start)
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1))
    expect(created).toHaveLength(1)
    expect(created[0].name).toMatch(/^spec-[0-9a-f]{8}$/)
    expect(created[0].name).toMatch(BACKEND_NAME_RE)
    expect(created[0].description).toBe('한국어로만 쓴 작업 설명')
    expect(onCreated).toHaveBeenCalledWith(created[0].name)
  })
})
