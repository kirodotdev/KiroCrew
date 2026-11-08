import { describe, it, expect, vi, afterEach, onTestFinished } from 'vitest'
import { act, screen, fireEvent, waitFor } from '@testing-library/react'
import i18next from 'i18next'

import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import SetupPanel, { activityLine } from './SetupPanel'

const BASE = '/api/apps/auto-improvement'
interface Call { method: string; path: string; body: unknown }

/** Route every fetch the panel makes. A Response is returned as-is; anything else is sent as JSON. */
function installApi(extra: (call: Call) => unknown = () => undefined) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const call: Call = {
      method: init?.method ?? 'GET',
      path: String(input).replace(BASE, ''),
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    }
    calls.push(call)
    const custom = await extra(call)
    if (custom instanceof Response) return custom
    const fallback =
      call.path === '/branches' ? { branches: ['main', 'dev'] }
        : call.path === '/run' && call.method === 'GET' ? { status: 'idle' }
          : { ok: true }
    return new Response(JSON.stringify(custom ?? fallback), { status: 200 })
  }))
  return calls
}

const CONFIGURED = { clone: { path: '/tmp/c' }, target_display: 'owner/repo', branch: 'main', directCommit: false }
const CLONE = i18nT('autoImprovement.cloneBtn')
const RUN = i18nT('autoImprovement.runBtn')
const STOP = i18nT('autoImprovement.stopBtn')
const REPO = i18nT('autoImprovement.repoLabel')
const AUTOCOMMIT = i18nT('autoImprovement.autocommitLabel')

afterEach(() => vi.unstubAllGlobals())

describe('activityLine branches', () => {
  it('formats every feed shape, with and without a timestamp', () => {
    expect(activityLine({ note: 'hello' })).toBe('hello')
    expect(activityLine({ t: 0, note: 'x' })).toBe('x')
    expect(activityLine({ t: 1785469717.28, note: 'hi' }).endsWith('  hi')).toBe(true)
    expect(activityLine({ error: 'boom' })).toBe('error: boom')
    expect(activityLine({ agent: { kind: 'tool' } })).toBe('tool')
    expect(activityLine({ agent: { detail: 'thinking' } })).toBe('agent — thinking')
    expect(activityLine({ agent: { kind: 'tool', tool: 'read', detail: 'b.py:1' } })).toBe('tool:read — b.py:1')
    expect(activityLine({ stage: 'profile' })).toBe('cycle ? · profile')
    expect(activityLine({ stage: 'profile', cycle: 4 })).toBe('cycle 4 · profile')
    const disc = i18nT('autoImprovement.activityDiscovered', { discovered: 5, fresh: 0 })
    expect(activityLine({ stage: 'propose', cycle: 2, discovered: 5 })).toBe(`cycle 2 · propose ${disc}`)
    expect(activityLine({ proposers: { fanned: 2 } } as Parameters<typeof activityLine>[0])).toBe('{"proposers":{"fanned":2}}')
  })
})

describe('SetupPanel repository and branch steps', () => {
  it('posts the trimmed URL, shows the backend error, and keeps clone disabled when empty', async () => {
    const calls = installApi((c) => (c.path === '/setup-clone' ? { ok: false, error: 'not a GitHub URL' } : undefined))
    renderWithProviders(<SetupPanel config={{}} />)
    expect(screen.getByRole('button', { name: CLONE })).toBeDisabled()
    fireEvent.change(screen.getByRole('textbox', { name: REPO }), { target: { value: '  https://github.com/o/r  ' } })
    fireEvent.click(screen.getByRole('button', { name: CLONE }))
    expect(await screen.findByText('not a GitHub URL')).toBeInTheDocument()
    expect(calls).toContainEqual({ method: 'POST', path: '/setup-clone', body: { url: 'https://github.com/o/r' } })
  })

  it('keeps keyboard focus on Clone while the clone runs, and ignores a second press (#33102)', async () => {
    let settle!: (value: Response) => void
    const calls = installApi((c) => (c.path === '/setup-clone'
      ? new Promise<Response>((resolve) => { settle = resolve })
      : undefined))
    renderWithProviders(<SetupPanel config={{}} />)
    fireEvent.change(screen.getByRole('textbox', { name: REPO }), { target: { value: 'https://github.com/o/r' } })
    const button = screen.getByRole('button', { name: CLONE })
    button.focus()
    // A double click in one tick, before the pending state renders.
    fireEvent.click(button)
    fireEvent.click(button)
    await waitFor(() => expect(button).toHaveAttribute('aria-disabled', 'true'))

    // A real browser drops focus from a disabled button; jsdom does not, so the
    // tab-order half is pinned on the attribute itself.
    expect(button).not.toBeDisabled()
    expect(button).toHaveFocus()
    fireEvent.click(button)
    fireEvent.keyDown(screen.getByRole('textbox', { name: REPO }), { key: 'Enter' })
    // Let any second mutation reach fetch before counting.
    await act(async () => {})
    expect(calls.filter((c) => c.path === '/setup-clone')).toHaveLength(1)

    settle(new Response(JSON.stringify({ ok: false, error: 'not a GitHub URL' })))
    expect(await screen.findByText('not a GitHub URL')).toBeInTheDocument()
    expect(button).not.toHaveAttribute('aria-disabled')
  })

  it('raises a mutation error when a failed clone returns no error text', async () => {
    installApi((c) => (c.path === '/setup-clone' ? new Response('{}', { status: 500 }) : undefined))
    const { queryClient } = renderWithProviders(<SetupPanel config={{}} />)
    fireEvent.change(screen.getByRole('textbox', { name: REPO }), { target: { value: 'https://github.com/o/r' } })
    fireEvent.click(screen.getByRole('button', { name: CLONE }))
    await waitFor(() => {
      const failed = queryClient.getMutationCache().getAll().find((m) => m.state.status === 'error')
      expect(failed?.state.error).toEqual(new Error('HTTP 500'))
    })
    // The failed request is announced, not dropped (#33102 review).
    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 500')
  })

  it('shows the repo and push-disabled badge, then saves the autocommit toggle', async () => {
    const calls = installApi()
    renderWithProviders(<SetupPanel config={CONFIGURED} />)
    expect(await screen.findByText('owner/repo')).toBeInTheDocument()
    expect(screen.getByText(i18nT('autoImprovement.pushDisabled'))).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('checkbox', { name: AUTOCOMMIT }))
    await waitFor(() => expect(calls).toContainEqual({ method: 'PUT', path: '/config', body: { directCommit: true } }))
  })

  it('puts the repo name where the language places it in the current-repo line', async () => {
    installApi()
    const key = 'autoImprovement.currentRepoLine'
    const shipped = i18next.getResource('ja', 'translation', key) as string
    onTestFinished(() => { i18next.addResource('ja', 'translation', key, shipped) })
    i18next.addResource('ja', 'translation', key, '<repo/> SENTINEL')
    await i18next.changeLanguage('ja')
    renderWithProviders(<SetupPanel config={CONFIGURED} />)
    const repo = await screen.findByText('owner/repo')
    expect(repo).toHaveClass('text-accent')
    expect(repo.closest('p')?.textContent).toMatch(/^owner\/repo SENTINEL/)
  })

  it('hides the branch step when the clone reports no branches', async () => {
    const calls = installApi((c) => (c.path === '/branches' ? { branches: [] } : undefined))
    const { queryClient } = renderWithProviders(<SetupPanel config={CONFIGURED} />)
    expect(await screen.findByText('owner/repo')).toBeInTheDocument()
    // The repo renders from props before the branch answer is read, so wait for that answer too.
    await waitFor(() => expect(queryClient.getQueryData(['auto-improvement-branches', true])).toEqual({ branches: [] }))
    expect(calls).toContainEqual({ method: 'GET', path: '/branches', body: undefined })
    expect(screen.queryByRole('checkbox', { name: AUTOCOMMIT })).toBeNull()
  })
})

describe('SetupPanel run control', () => {
  it('starts a run when idle', async () => {
    const calls = installApi()
    renderWithProviders(<SetupPanel config={CONFIGURED} />)
    fireEvent.click(await screen.findByRole('button', { name: RUN }))
    await waitFor(() => expect(calls).toContainEqual({ method: 'POST', path: '/run', body: {} }))
  })

  it('shows live counts and only the last 30 activity lines, then stops the run', async () => {
    const activity = Array.from({ length: 35 }, (_, i) => ({ note: `step ${i}` }))
    const calls = installApi((c) =>
      c.path === '/run' && c.method === 'GET' ? { status: 'running', cycle: 3, kept: 1, drafted: 2, activity } : undefined,
    )
    renderWithProviders(<SetupPanel config={CONFIGURED} />)
    fireEvent.click(await screen.findByRole('button', { name: STOP }))
    await waitFor(() => expect(calls).toContainEqual({ method: 'POST', path: '/run/stop', body: {} }))
    expect(screen.getByText(i18nT('autoImprovement.runningStatus', { cycle: 3, kept: 1, drafted: 2 }))).toBeInTheDocument()
    expect(screen.getByText('step 5')).toBeInTheDocument()
    expect(screen.queryByText('step 4')).toBeNull()
  })

  it('shows the run error, or the generic message when the error has no text', async () => {
    installApi((c) => (c.path === '/run' && c.method === 'GET' ? { status: 'error', error: 'clone vanished' } : undefined))
    const { unmount } = renderWithProviders(<SetupPanel config={CONFIGURED} />)
    expect(await screen.findByText('clone vanished')).toBeInTheDocument()
    unmount()
    installApi((c) => (c.path === '/run' && c.method === 'GET' ? { status: 'error' } : undefined))
    renderWithProviders(<SetupPanel config={CONFIGURED} />)
    expect(await screen.findByText(i18nT('autoImprovement.runError'))).toBeInTheDocument()
  })
})
