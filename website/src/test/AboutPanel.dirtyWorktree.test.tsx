//
// Contract under test: Settings > About, gateway Update refused on a dirty
// checkout.
//
// The gateway refuses `POST /api/update` with 409 when the checkout has
// uncommitted changes. With `code: "dirty_worktree"` the dialog explains why
// it stopped and what to do (commit or stash), and shows a stash command.
// Any other 409 keeps showing the server's own text, with no stash command.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { sseStatus } from '../store/dashboardSlice'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'

const BLANK_STATUS = {
  uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
} as const

const CHECK = {
  check_status: 'succeeded',
  update_available: true,
  error_code: null,
  managed_by: 'git',
  can_apply: true,
  latest_version: '0.1.3',
}

function stubFetch(applyBody: Record<string, unknown>) {
  const answer = (status: number, body: unknown) => ({
    ok: status < 400,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: new Headers({ 'content-type': 'application/json' }),
  })
  vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
    const url = String(input)
    if (url.includes('/api/update/check')) return answer(200, CHECK)
    if (url.includes('/api/changelog')) return answer(200, { content: '' })
    if (/\/api\/update(\?|$)/.test(url)) return answer(409, applyBody)
    return answer(200, {})
  }))
}

function mountWeb() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AboutPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

async function pressUpdateNow() {
  const trigger = await screen.findByRole('button', { name: /^Update(?! the gateway)/ })
  fireEvent.click(trigger)
  const dialog = await screen.findByRole('dialog')
  const apply = await waitFor(() => within(dialog).getByRole('button', { name: /^Update now$/i }))
  fireEvent.click(apply)
}

describe('AboutPanel update refused on a dirty checkout', () => {
  beforeEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
    store.dispatch(sseStatus({ ...BLANK_STATUS, update_available: true, update_can_apply: true } as never))
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('dirty_worktree shows guided copy with the count and a stash command', async () => {
    stubFetch({
      error: 'Working tree has uncommitted changes — commit or stash first',
      code: 'dirty_worktree',
      changed: 3,
    })
    mountWeb()
    await pressUpdateNow()

    const error = await screen.findByTestId('update-apply-error')
    expect(error.textContent).toContain('keep your local edits safe')
    expect(error.textContent).toContain('(3)')
    expect(error.textContent).toContain('Commit or stash them')
    expect(error.textContent).toContain('press Update now again')
    expect(error.textContent).toContain('in your Kiro Crew folder')
    const command = screen.getByTestId('update-dirty-stash-command')
    expect(command.querySelector('code')?.textContent).toBe('git stash push -u -m "before kirocrew update"')
    // The shared copy block, so it copies like every other command in the app.
    expect(within(command).getByRole('button', { name: /copy command/i })).toBeTruthy()
  })

  it('any other 409 shows the server text and no stash command', async () => {
    stubFetch({ error: 'Not a git checkout — update this install with `kirocrew update`' })
    mountWeb()
    await pressUpdateNow()

    const error = await screen.findByTestId('update-apply-error')
    expect(error.textContent).toContain('Not a git checkout')
    expect(screen.queryByTestId('update-dirty-stash-command')).toBeNull()
  })
})
