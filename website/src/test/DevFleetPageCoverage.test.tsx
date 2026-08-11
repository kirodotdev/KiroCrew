/**
 * DevFleetPage — coverage for the interaction paths the smoke suite leaves
 * cold: row-action side effects (pod up/down/restart/open, rebase, remove),
 * the detail panel's rich payload, sync/provision poll terminal states, the
 * prune failure branches, the make-live error banner, and the small render
 * helpers (relative time buckets, unknown PR state, legacy toggle).
 *
 * Every test drives the real component through fetch, so a route that stops
 * being called (or starts being called with the wrong shape) fails here.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

import DevFleetPage from '../pages/DevFleetPage'

type Body = Record<string, unknown> | unknown[] | null
type RouteHandler = (u: string, opts?: RequestInit) => Response | Promise<Response> | null

const res = (body: Body, status = 200) => new Response(JSON.stringify(body), { status })

/**
 * Install a fetch double. `handler` gets first refusal on every URL; returning
 * null falls through to the shared /fleet + /disk defaults, which every test
 * needs and none of them is about.
 */
function installFetch(fleet: Body, handler?: RouteHandler) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((url, opts) => {
    const u = typeof url === 'string' ? url : url instanceof URL ? url.toString() : (url as Request).url
    const custom = handler?.(u, opts as RequestInit | undefined)
    if (custom) return Promise.resolve(custom)
    if (u.includes('/fleet')) return Promise.resolve(res(fleet))
    if (u.includes('/disk')) return Promise.resolve(res({ total_mb: 51200 }))
    return Promise.resolve(res({}))
  })
}

const isPost = (opts?: RequestInit) => String(opts?.method || '').toUpperCase() === 'POST'
const nowSec = () => Math.floor(Date.now() / 1000)

const MAIN_ROW = { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, branch: 'main', is_live: true }

/** A non-main row that is built, idle, and eligible for every row action. */
const readyRow = (over: Record<string, unknown> = {}) => ({
  name: 'wt-a', is_main: false, running: false, has_dist: true, behind: 0,
  branch: 'feat/a', path: '/w/wt-a', last_updated_at: nowSec(), ...over,
})

const fleetOf = (...worktrees: Record<string, unknown>[]) => ({ base_branch: 'main', worktrees })

function renderPage() {
  return renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
}

// DevFleetPage's ToastHost schedules a 4s (7s for errors) window.setTimeout to
// auto-dismiss each toast, and its effect cleanup only drops the listener -- it
// never clears those timers. Every test here that raises a toast therefore
// leaves a pending callback that calls setToasts long after the test ends; once
// vitest tears the environment down, that callback hits a torn-down global and
// throws "ReferenceError: window is not defined" as an UNHANDLED error. Vitest
// reports every test as passing and still exits non-zero, so this fails CI
// without failing a test.
//
// Fake timers keep those callbacks off the real clock and clearAllTimers drops
// the pending ones at teardown. `shouldAdvanceTime` keeps the clock moving on
// its own so waitFor and the polling effects behave as they do with real timers.
beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
})
afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
})

/** Wait for the first paint of the fleet table. */
async function waitForRow(name: string) {
  await waitFor(() => expect(screen.getByText(name)).toBeInTheDocument(), { timeout: 4000 })
}

/** Open a non-main row's portaled actions menu and return it. */
async function openRowMenu() {
  fireEvent.click(screen.getByLabelText('More actions'))
  return within(await screen.findByRole('menu'))
}

describe('DevFleetPage row rendering helpers', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('renders each relative-time bucket for the UPDATED column', async () => {
    // relTime() has four buckets past "just now"; the row strips the " ago"
    // suffix, so the column reads 5m / 3d / 2mo.
    installFetch(fleetOf(
      MAIN_ROW,
      readyRow({ name: 'wt-mins', path: '/w/1', last_updated_at: nowSec() - 300 }),
      readyRow({ name: 'wt-days', path: '/w/2', last_updated_at: nowSec() - 3 * 86400 }),
      readyRow({ name: 'wt-months', path: '/w/3', last_updated_at: nowSec() - 70 * 86400 }),
    ))
    renderPage()
    await waitForRow('wt-mins')
    expect(screen.getByText('5m')).toBeInTheDocument()
    expect(screen.getByText('3d')).toBeInTheDocument()
    expect(screen.getByText('2mo')).toBeInTheDocument()
  })

  it('renders an ellipsis PR pill for a state the UI does not model, and still sorts', async () => {
    // Two non-main rows force the comparator to run, so prRank sees the
    // unknown state too — it must rank with the PR-less rows rather than throw.
    installFetch(fleetOf(
      readyRow({ name: 'wt-open', path: '/w/1', pr: { number: 1, state: 'OPEN', url: 'https://example.test/pr/1' } }),
      readyRow({ name: 'wt-weird', path: '/w/2', pr: { number: 2, state: 'SOMETHING_NEW', url: 'https://example.test/pr/2' } }),
    ))
    renderPage()
    await waitForRow('wt-weird')
    expect(screen.getByText('\u2026')).toBeInTheDocument()
    expect(screen.getByText('open')).toBeInTheDocument()
  })

  it('hides legacy worktrees behind a toggle that reveals them on click', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow({ name: 'wt-old', path: '/w/old', legacy: true })))
    renderPage()
    await waitFor(() => expect(screen.getByText(/legacy worktrees hidden/)).toBeInTheDocument(), { timeout: 4000 })
    expect(screen.queryByText('wt-old')).toBeNull()
    fireEvent.click(screen.getByText(/legacy worktrees hidden/))
    expect(screen.getByText('wt-old')).toBeInTheDocument()
    expect(screen.getByText(/Hide 1 legacy worktrees/)).toBeInTheDocument()
  })

  it('offers Open plus the pod lifecycle menu items for a running, built worktree', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow({ running: true, health: 200 })))
    renderPage()
    await waitForRow('wt-a')
    expect(screen.getByText('Open')).toBeInTheDocument()
    const menu = await openRowMenu()
    expect(menu.getByText('Restart pod')).toBeInTheDocument()
    expect(menu.getByText('Stop pod')).toBeInTheDocument()
    // Spin up is the mutually exclusive one — the pod is already running.
    expect(menu.queryByText('Spin up pod')).toBeNull()
  })
})

describe('DevFleetPage portaled popovers', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('closes the row menu on a scroll, because its position is fixed', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()))
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByLabelText('More actions'))
    expect(await screen.findByRole('menu')).toBeInTheDocument()
    fireEvent.scroll(window)
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
  })

  it('closes the confirm popover on a resize and on Cancel', async () => {
    installFetch(fleetOf(MAIN_ROW))
    renderPage()
    await waitFor(() => expect(screen.getByText('Pull+Build')).toBeInTheDocument(), { timeout: 4000 })
    const trigger = screen.getByText('Pull+Build').closest('button') as HTMLButtonElement

    fireEvent.click(trigger)
    expect(await screen.findByRole('dialog', { name: 'Pull + Build main' })).toBeInTheDocument()
    fireEvent.resize(window)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    fireEvent.click(trigger)
    const pop = await screen.findByRole('dialog', { name: 'Pull + Build main' })
    fireEvent.click(within(pop).getByText('Cancel'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })
})

describe('DevFleetPage detail panel', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  const DETAIL = {
    branch: 'feat/a',
    design_docs: ['docs/rfc-a.md', 'docs/rfc-b.md'],
    commits: [{ hash: 'abc1234', subject: 'add the thing', when: '2 days ago' }],
    disk_mb: 512,
    pod_running: true,
    pod_port: 7791,
    own_commits: 1,
    real_dirty: true,
  }

  it('renders design docs, commits and the pod-log control from the detail payload', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow({ running: true, health: 200 })), (u) => {
      if (u.includes('/worktree?name=')) return res(DETAIL)
      if (u.includes('/pod/logs')) return res({ ok: true, logs: 'pod line one\npod line two' })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByLabelText('Expand'))

    await waitFor(() => expect(screen.getByText('docs/rfc-a.md')).toBeInTheDocument(), { timeout: 4000 })
    expect(screen.getByText('docs/rfc-b.md')).toBeInTheDocument()
    expect(screen.getByText('abc1234')).toBeInTheDocument()
    expect(screen.getByText('add the thing')).toBeInTheDocument()
    expect(screen.getByText('2 days ago')).toBeInTheDocument()
    expect(screen.getByText(/Disk:\s*512\s*MB/)).toBeInTheDocument()

    // The pod-log control only exists while the pod is running.
    fireEvent.click(screen.getByText('Load pod logs'))
    await waitFor(() => expect(screen.getByText(/pod line one/)).toBeInTheDocument())
    // Second click collapses without refetching.
    fireEvent.click(screen.getByText('Hide logs'))
    await waitFor(() => expect(screen.queryByText(/pod line one/)).toBeNull())
  })

  it('reports a pod-log failure instead of rendering an empty panel', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow({ running: true, health: 200 })), (u) => {
      if (u.includes('/worktree?name=')) return res(DETAIL)
      if (u.includes('/pod/logs')) return res({ ok: false, error: 'journalctl unavailable' })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByLabelText('Expand'))
    await waitFor(() => expect(screen.getByText('Load pod logs')).toBeInTheDocument(), { timeout: 4000 })
    fireEvent.click(screen.getByText('Load pod logs'))
    await waitFor(() => expect(screen.getByText('journalctl unavailable')).toBeInTheDocument())
  })

  it('reports a pod-log transport failure', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow({ running: true, health: 200 })), (u) => {
      if (u.includes('/worktree?name=')) return res(DETAIL)
      if (u.includes('/pod/logs')) return Promise.reject(new Error('log stream refused'))
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByLabelText('Expand'))
    await waitFor(() => expect(screen.getByText('Load pod logs')).toBeInTheDocument(), { timeout: 4000 })
    fireEvent.click(screen.getByText('Load pod logs'))
    await waitFor(() => expect(screen.getByText('log stream refused')).toBeInTheDocument())
  })

  it('surfaces a detail fetch failure on the row instead of an empty panel', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u) => {
      if (u.includes('/worktree?name=')) return Promise.reject(new Error('detail backend down'))
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByLabelText('Expand'))
    await waitFor(() => expect(screen.getByText('detail backend down')).toBeInTheDocument(), { timeout: 4000 })
  })
})

describe('DevFleetPage remove worktree', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('removes an empty worktree after the non-destructive confirm copy', async () => {
    const removeBodies: string[] = []
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u, opts) => {
      if (u.includes('/worktree/remove')) { removeBodies.push(String(opts?.body)); return res({ ok: true }) }
      if (u.includes('/worktree?name=')) return res({ branch: 'feat/a', own_commits: 0, real_dirty: false })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByLabelText('Expand'))
    await waitFor(() => expect(screen.getByText('Remove')).toBeInTheDocument(), { timeout: 4000 })
    fireEvent.click(screen.getByText('Remove'))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/Empty worktree/)).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(screen.getByText('Removed wt-a')).toBeInTheDocument())
    // An empty checkout needs no --force.
    expect(removeBodies[0]).toContain('"force":false')
  })

  it('forces removal of unmerged work only behind the Delete anyway confirm, and reports refusal', async () => {
    const removeBodies: string[] = []
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u, opts) => {
      if (u.includes('/worktree/remove')) { removeBodies.push(String(opts?.body)); return res({ ok: false, error: 'worktree is locked' }) }
      if (u.includes('/worktree?name=')) return res({ branch: 'feat/a', own_commits: 4, real_dirty: true })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByLabelText('Expand'))
    await waitFor(() => expect(screen.getByText('Remove')).toBeInTheDocument(), { timeout: 4000 })
    fireEvent.click(screen.getByText('Remove'))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/Has unmerged work/)).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete anyway' }))
    await waitFor(() => expect(screen.getByText('worktree is locked')).toBeInTheDocument())
    expect(removeBodies[0]).toContain('"force":true')
  })

  it('reports a transport failure on the removal', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u) => {
      if (u.includes('/worktree/remove')) return Promise.reject(new Error('remove endpoint down'))
      if (u.includes('/worktree?name=')) return res({ branch: 'feat/a', shipped: true })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByLabelText('Expand'))
    await waitFor(() => expect(screen.getByText('Remove')).toBeInTheDocument(), { timeout: 4000 })
    fireEvent.click(screen.getByText('Remove'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(screen.getByText('remove endpoint down')).toBeInTheDocument())
  })

  it('abandons the removal when the confirm is cancelled', async () => {
    let removeCalls = 0
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u) => {
      if (u.includes('/worktree/remove')) { removeCalls++; return res({ ok: true }) }
      if (u.includes('/worktree?name=')) return res({ branch: 'feat/a', shipped: true })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByLabelText('Expand'))
    await waitFor(() => expect(screen.getByText('Remove')).toBeInTheDocument(), { timeout: 4000 })
    fireEvent.click(screen.getByText('Remove'))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/PR merged/)).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(removeCalls).toBe(0)
  })
})

describe('DevFleetPage rebase', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  async function confirmRebase() {
    const menu = await openRowMenu()
    fireEvent.click(menu.getByText('Rebase onto main'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Rebase' }))
  }

  it('shows the new HEAD inline on success and clears it when dismissed', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u, opts) => {
      if (u.includes('/rebase') && isPost(opts)) return res({ ok: true, head: 'deadbeefcafe', ahead: 2, behind: 0 })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    await confirmRebase()
    // Toast and inline row marker both carry the short HEAD.
    await waitFor(() => expect(screen.getAllByText('Rebased (HEAD deadbee)').length).toBeGreaterThan(0))
    fireEvent.click(screen.getByLabelText('Dismiss'))
    await waitFor(() => expect(screen.queryByLabelText('Dismiss')).toBeNull())
  })

  it('reports an aborted rebase distinctly from a hard failure', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u, opts) => {
      if (u.includes('/rebase') && isPost(opts)) return res({ ok: false, conflict: true })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    await confirmRebase()
    await waitFor(() => expect(screen.getByText('Conflicts \u2014 aborted')).toBeInTheDocument())
    expect(screen.getByText('Rebase conflicts')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Dismiss'))
  })

  it('surfaces the server error text for a failed rebase', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u, opts) => {
      if (u.includes('/rebase') && isPost(opts)) return res({ ok: false, error: 'dirty working tree' })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    await confirmRebase()
    await waitFor(() => expect(screen.getAllByText('dirty working tree').length).toBeGreaterThan(0))
    fireEvent.click(screen.getByLabelText('Dismiss'))
  })

  it('reports a transport failure without leaving an inline marker behind', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u, opts) => {
      if (u.includes('/rebase') && isPost(opts)) return Promise.reject(new Error('rebase endpoint down'))
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    await confirmRebase()
    await waitFor(() => expect(screen.getByText('rebase endpoint down')).toBeInTheDocument())
    // A throw produces no verdict, so there is nothing to dismiss on the row.
    expect(screen.queryByLabelText('Dismiss')).toBeNull()
  })

  it('does not touch the branch when the rebase confirm is cancelled', async () => {
    let rebaseCalls = 0
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u, opts) => {
      if (u.includes('/rebase') && isPost(opts)) { rebaseCalls++; return res({ ok: true, head: 'aaaaaaa' }) }
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    const menu = await openRowMenu()
    fireEvent.click(menu.getByText('Rebase onto main'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(rebaseCalls).toBe(0)
  })
})

describe('DevFleetPage pod actions', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  function stubWindowOpen() {
    const fake = { opener: {} as unknown, location: { href: '' }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(fake as unknown as Window)
    return fake
  }

  it('opens the pod in a severed tab once the URL comes back', async () => {
    const fake = stubWindowOpen()
    installFetch(fleetOf(MAIN_ROW, readyRow({ running: true, health: 200 })), (u, opts) => {
      if (u.includes('/pod/') && isPost(opts)) return res({ ok: true, url: 'https://pod.example.test/?k=1' })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByText('Open'))
    await waitFor(() => expect(fake.location.href).toBe('https://pod.example.test/?k=1'))
    // The pod runs worktree code under test — it must not keep a handle back.
    expect(fake.opener).toBeNull()
  })

  it('closes the blank tab and reports the error when the mint fails', async () => {
    const fake = stubWindowOpen()
    installFetch(fleetOf(MAIN_ROW, readyRow({ running: true, health: 200 })), (u, opts) => {
      if (u.includes('/pod/') && isPost(opts)) return res({ ok: false, error: 'mint refused' })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByText('Open'))
    await waitFor(() => expect(screen.getByText('mint refused')).toBeInTheDocument())
    expect(fake.close).toHaveBeenCalled()
  })

  it('spins a pod up from the row menu and confirms it came up', async () => {
    const posted: string[] = []
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u, opts) => {
      if (u.includes('/pod/up') && isPost(opts)) { posted.push(u); return res({ ok: true }) }
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    const menu = await openRowMenu()
    fireEvent.click(menu.getByText('Spin up pod'))
    await waitFor(() => expect(screen.getByText('Pod up: wt-a')).toBeInTheDocument())
    expect(posted).toHaveLength(1)
  })

  it('reports a pod start failure with the server reason', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u, opts) => {
      if (u.includes('/pod/up') && isPost(opts)) return res({ ok: false, error: 'port 7791 in use' })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    const menu = await openRowMenu()
    fireEvent.click(menu.getByText('Spin up pod'))
    await waitFor(() => expect(screen.getByText('port 7791 in use')).toBeInTheDocument())
  })

  it('stops a running pod from the row menu', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow({ running: true, health: 200 })), (u, opts) => {
      if (u.includes('/pod/down') && isPost(opts)) return res({ ok: true })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    const menu = await openRowMenu()
    fireEvent.click(menu.getByText('Stop pod'))
    await waitFor(() => expect(screen.getByText('Stopped wt-a')).toBeInTheDocument())
  })

  it('restarts a running pod from the row menu', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow({ running: true, health: 200 })), (u, opts) => {
      if (u.includes('/pod/restart') && isPost(opts)) return res({ ok: true })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    const menu = await openRowMenu()
    fireEvent.click(menu.getByText('Restart pod'))
    await waitFor(() => expect(screen.getByText('Restarted wt-a')).toBeInTheDocument())
  })

  it('reports a transport failure from a pod action', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow({ running: true, health: 200 })), (u, opts) => {
      if (u.includes('/pod/down') && isPost(opts)) return Promise.reject(new Error('socket hang up'))
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    const menu = await openRowMenu()
    fireEvent.click(menu.getByText('Stop pod'))
    await waitFor(() => expect(screen.getByText('socket hang up')).toBeInTheDocument())
  })

  it('falls back to a named window when the blank tab is blocked', async () => {
    // A blocked popup returns null, so there is no handle to point at the pod —
    // the URL has to be opened on its own, still without an opener.
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null)
    installFetch(fleetOf(MAIN_ROW, readyRow({ running: true, health: 200 })), (u, opts) => {
      if (u.includes('/pod/') && isPost(opts)) return res({ ok: true, url: 'https://pod.example.test/late' })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByText('Open'))
    await waitFor(() => expect(openSpy).toHaveBeenCalledTimes(2))
    expect(openSpy).toHaveBeenLastCalledWith('https://pod.example.test/late', '_blank', 'noopener')
  })

  it('hands the QA run to a fresh chat session with the worktree name in the prompt', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()))
    const { store } = renderPage()
    await waitForRow('wt-a')
    const menu = await openRowMenu()
    fireEvent.click(menu.getByText('QA + video'))
    await waitFor(() => expect(store.getState().chat.pendingInput).toContain("worktree 'wt-a'"))
    expect(store.getState().chat.pendingInput).toContain('pod-e2e')
  })
})

describe('DevFleetPage sync run lifecycle', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  async function startSync() {
    const trigger = screen.getByText('Pull+Build').closest('button') as HTMLButtonElement
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('button', { name: 'Start' }))
  }

  it('renders the success stepper and a filtered log when the run finishes clean', async () => {
    installFetch(fleetOf(MAIN_ROW), (u, opts) => {
      if (u.includes('/sync') && isPost(opts)) return res({ ok: true, run_id: 'sync-ok' })
      if (u.includes('/run?id=sync-ok')) {
        return res({ status: 'done', exit_code: 0, output: ['::step::5::restart', 'build finished'], started: nowSec() - 20 })
      }
      return null
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Pull+Build')).toBeInTheDocument(), { timeout: 4000 })
    await startSync()
    await waitFor(
      () => expect(screen.getByText('restart gateway to apply the new build')).toBeInTheDocument(),
      { timeout: 8000 },
    )
    // The step markers are protocol, not output — the panel must not show them.
    fireEvent.click(screen.getByLabelText('Toggle log'))
    const pre = await waitFor(() => document.querySelector('pre') as HTMLPreElement)
    expect(pre.textContent).toContain('build finished')
    expect(pre.textContent).not.toContain('::step::')
    fireEvent.click(screen.getByLabelText('Dismiss sync status'))
    await waitFor(() => expect(screen.queryByLabelText('Dismiss sync status')).toBeNull())
  }, 20000)

  it('renders the failure stepper with the last output line when the run exits non-zero', async () => {
    installFetch(fleetOf(MAIN_ROW), (u, opts) => {
      if (u.includes('/sync') && isPost(opts)) return res({ ok: true, run_id: 'sync-bad' })
      if (u.includes('/run?id=sync-bad')) {
        return res({ status: 'done', exit_code: 2, output: ['npm ci failed'], started: nowSec() - 5 })
      }
      return null
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Pull+Build')).toBeInTheDocument(), { timeout: 4000 })
    await startSync()
    await waitFor(() => expect(screen.getByText('Pull+Build failed')).toBeInTheDocument(), { timeout: 8000 })
    expect(screen.getByText('Pull+Build failed (exit 2): npm ci failed')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Toggle log'))
    expect((document.querySelector('pre') as HTMLPreElement).textContent).toContain('npm ci failed')
    fireEvent.click(screen.getByLabelText('Dismiss sync status'))
    await waitFor(() => expect(screen.queryByLabelText('Dismiss sync status')).toBeNull())
  }, 20000)

  it('declares the run lost when the registry 404s mid-poll instead of freezing the bar', async () => {
    installFetch(fleetOf(MAIN_ROW), (u, opts) => {
      if (u.includes('/sync') && isPost(opts)) return res({ ok: true, run_id: 'sync-gone' })
      if (u.includes('/run?id=sync-gone')) return res({ error: 'unknown run' }, 404)
      return null
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Pull+Build')).toBeInTheDocument(), { timeout: 4000 })
    await startSync()
    await waitFor(
      () => expect(screen.getByText('Sync run lost (gateway restarted mid-sync). Re-run Pull+Build.')).toBeInTheDocument(),
      { timeout: 8000 },
    )
    expect(screen.getByText(/run lost; check git state/)).toBeInTheDocument()
  }, 20000)

  it('reports a transport failure on the sync POST without leaving the button stuck', async () => {
    installFetch(fleetOf(MAIN_ROW), (u, opts) => {
      if (u.includes('/sync') && isPost(opts)) return Promise.reject(new Error('gateway unreachable'))
      return null
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Pull+Build')).toBeInTheDocument(), { timeout: 4000 })
    await startSync()
    await waitFor(() => expect(screen.getByText('gateway unreachable')).toBeInTheDocument())
    expect(screen.getByText('Pull+Build').closest('button')).not.toBeDisabled()
  })

  it('lets a reattached in-flight run be inspected and dismissed', async () => {
    installFetch({ ...fleetOf(MAIN_ROW), sync_run_id: 'sync-live' }, (u) => {
      if (u.includes('/run?id=sync-live')) {
        return res({ status: 'running', output: ['::step::3::npm ci', 'installing deps'], started: nowSec() - 45, step_label: '3/5 npm ci' })
      }
      return null
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Syncing')).toBeInTheDocument(), { timeout: 6000 })
    expect(screen.getByText('3/5 npm ci')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Toggle log'))
    const pre = await waitFor(() => document.querySelector('pre') as HTMLPreElement)
    expect(pre.textContent).toContain('installing deps')
    fireEvent.click(screen.getByLabelText('Dismiss sync status'))
    await waitFor(() => expect(screen.queryByText('Syncing')).toBeNull())
  }, 15000)
})

describe('DevFleetPage provision failures', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  const unbuilt = readyRow({ name: 'wt-new', has_dist: false, path: '/w/new' })

  it('keeps a visible failed stepper when the run never starts', async () => {
    installFetch(fleetOf(MAIN_ROW, unbuilt), (u, opts) => {
      if (u.includes('/pod/provision') && isPost(opts)) return res({ ok: false })
      return null
    })
    renderPage()
    await waitForRow('wt-new')
    fireEvent.click(screen.getByText('Provision'))
    await waitFor(() => expect(screen.getByText('Provision failed')).toBeInTheDocument())
    // Both the toast and the stepper's last-output line carry the reason.
    expect(screen.getAllByText('Provision failed to start').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByLabelText('Dismiss provision status'))
    await waitFor(() => expect(screen.queryByText('Provision failed')).toBeNull())
  })

  it('keeps a visible failed stepper when the provision POST cannot be sent', async () => {
    installFetch(fleetOf(MAIN_ROW, unbuilt), (u, opts) => {
      if (u.includes('/pod/provision') && isPost(opts)) return Promise.reject(new Error('provision endpoint down'))
      return null
    })
    renderPage()
    await waitForRow('wt-new')
    fireEvent.click(screen.getByText('Provision'))
    await waitFor(() => expect(screen.getByText('Provision failed')).toBeInTheDocument())
    expect(screen.getAllByText('provision endpoint down').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByLabelText('Dismiss provision status'))
  })

  it('rides out unreadable polls and treats a non-running status as terminal', async () => {
    // Poll 1 throws, poll 2 returns no run at all, poll 3 reports the timeout —
    // only the third is terminal, and the first two must not end the run.
    let polls = 0
    installFetch(fleetOf(MAIN_ROW, unbuilt), (u, opts) => {
      if (u.includes('/pod/provision') && isPost(opts)) return res({ ok: true, run_id: 'prov-1' })
      if (u.includes('/run?id=prov-1')) {
        polls++
        if (polls === 1) return Promise.reject(new Error('poll blipped'))
        if (polls === 2) return res(null)
        return res({ status: 'timeout', output: ['pip install still running'] })
      }
      return null
    })
    renderPage()
    await waitForRow('wt-new')
    fireEvent.click(screen.getByText('Provision'))
    await waitFor(() => expect(screen.getByText('Provision timed out')).toBeInTheDocument(), { timeout: 14000 })
    expect(polls).toBeGreaterThanOrEqual(3)
    // The accumulated output is retained and auto-expanded on failure.
    expect(screen.getAllByText('pip install still running').length).toBeGreaterThan(0)
  }, 25000)
})

describe('DevFleetPage prune failures', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  const CANDIDATES = { ok: true, candidates: [{ name: 'wt-a', code: 'merged' }], kept: [], scanned: 1 }

  it('reports a refused preview', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u) => {
      if (u.includes('/prune-candidates')) return res({ ok: false, error: 'git ls-remote failed' })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByText('Prune merged'))
    await waitFor(() => expect(screen.getByText('git ls-remote failed')).toBeInTheDocument())
    expect(screen.queryByText('Prune worktrees')).toBeNull()
  })

  it('says so plainly when the scan finds neither candidates nor kept rows', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u) => {
      if (u.includes('/prune-candidates')) return res({ ok: true, candidates: [], kept: [], scanned: 0 })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByText('Prune merged'))
    await waitFor(() => expect(screen.getByText('Nothing to prune')).toBeInTheDocument())
    expect(screen.queryByText('Prune worktrees')).toBeNull()
  })

  it('reports a transport failure on the preview', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u) => {
      if (u.includes('/prune-candidates')) return Promise.reject(new Error('scan transport failed'))
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByText('Prune merged'))
    await waitFor(() => expect(screen.getByText('scan transport failed')).toBeInTheDocument())
  })

  it('refuses to run with every candidate deselected', async () => {
    let runCalls = 0
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u) => {
      if (u.includes('/prune-candidates')) return res(CANDIDATES)
      if (u.includes('/prune-run')) { runCalls++; return res({ ok: true }) }
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByText('Prune merged'))
    await waitFor(() => expect(screen.getByText('Prune worktrees')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Select wt-a'))
    fireEvent.click(screen.getByText('Remove selected'))
    await waitFor(() => expect(screen.getByText('Nothing selected')).toBeInTheDocument())
    expect(runCalls).toBe(0)
  })

  it('drops the checklist when the run is rejected, rather than showing every row Pending', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u) => {
      if (u.includes('/prune-candidates')) return res(CANDIDATES)
      if (u.includes('/prune-run')) return res({ ok: false, error: 'prune already running' })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByText('Prune merged'))
    await waitFor(() => expect(screen.getByText('Prune worktrees')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Remove selected'))
    await waitFor(() => expect(screen.getByText('prune already running')).toBeInTheDocument())
    expect(screen.queryByText('Pruning worktrees')).toBeNull()
  })

  it('drops the checklist when the run POST cannot be sent', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u) => {
      if (u.includes('/prune-candidates')) return res(CANDIDATES)
      if (u.includes('/prune-run')) return Promise.reject(new Error('prune transport failed'))
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByText('Prune merged'))
    await waitFor(() => expect(screen.getByText('Prune worktrees')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Remove selected'))
    await waitFor(() => expect(screen.getByText('prune transport failed')).toBeInTheDocument())
    expect(screen.queryByText('Pruning worktrees')).toBeNull()
  })

  it('terminates a name the backend never tracked as an explained failure', async () => {
    // The worktree vanished between preview and execute, so it is absent from
    // the status payload — it must not sit "Pending" in a finished checklist.
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u) => {
      if (u.includes('/prune-candidates')) return res(CANDIDATES)
      if (u.includes('/prune-run')) return res({ ok: true })
      if (u.includes('/prune-status')) return res({ running: false, done: 1, items: {} })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByText('Prune merged'))
    await waitFor(() => expect(screen.getByText('Prune worktrees')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Remove selected'))
    await waitFor(
      () => expect(screen.getByTestId('prune-item-wt-a')).toHaveAttribute('data-status', 'failed'),
      { timeout: 8000 },
    )
    expect(screen.getByText('not processed (unknown or no longer a worktree)')).toBeInTheDocument()
    expect(screen.getByText('Prune complete')).toBeInTheDocument()
  }, 15000)

  it('rides out an unreadable status poll instead of abandoning the run', async () => {
    let statusPolls = 0
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u) => {
      if (u.includes('/prune-candidates')) return res(CANDIDATES)
      if (u.includes('/prune-run')) return res({ ok: true })
      if (u.includes('/prune-status')) {
        statusPolls++
        if (statusPolls === 1) return Promise.reject(new Error('status blipped'))
        if (statusPolls === 2) return res(null)
        return res({ running: false, done: 1, items: { 'wt-a': { status: 'done', error: null } } })
      }
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    fireEvent.click(screen.getByText('Prune merged'))
    await waitFor(() => expect(screen.getByText('Prune worktrees')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Remove selected'))
    await waitFor(
      () => expect(screen.getByTestId('prune-item-wt-a')).toHaveAttribute('data-status', 'done'),
      { timeout: 12000 },
    )
    expect(statusPolls).toBeGreaterThanOrEqual(3)
    expect(screen.getByText('Prune complete')).toBeInTheDocument()
  }, 20000)
})

describe('DevFleetPage make live', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  async function confirmMakeLive() {
    const menu = await openRowMenu()
    fireEvent.click(menu.getByText('Make live'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /Make live/i }))
  }

  it('refuses a cutover to a worktree whose path the backend did not report', async () => {
    let posts = 0
    installFetch(fleetOf(MAIN_ROW, readyRow({ path: undefined })), (u, opts) => {
      if (u.includes('/make-live') && isPost(opts)) { posts++; return res({ ok: true }) }
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    const menu = await openRowMenu()
    fireEvent.click(menu.getByText('Make live'))
    await waitFor(() => expect(screen.getByText('Cannot resolve worktree path for wt-a')).toBeInTheDocument())
    // No confirm was even offered, and nothing was staged.
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(posts).toBe(0)
  })

  it('keeps a refused cutover on the page in a persistent banner', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u, opts) => {
      if (u.includes('/make-live') && isPost(opts)) return res({ ok: false, error: 'worktree is not built — run Provision first' })
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    await confirmMakeLive()
    const banner = await waitFor(() => screen.getByTestId('gateway-restart-error'))
    expect(banner).toHaveTextContent('worktree is not built')
  })

  it('names the failed operation when the cutover POST cannot be sent', async () => {
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u, opts) => {
      if (u.includes('/make-live') && isPost(opts)) return Promise.reject(new Error('Failed to fetch'))
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    await confirmMakeLive()
    const banner = await waitFor(() => screen.getByTestId('gateway-restart-error'))
    // Bare transport text says nothing on its own — the banner must lead with
    // what failed.
    expect(banner).toHaveTextContent('Make live failed: Failed to fetch')
  })

  it('stages nothing when the cutover confirm is cancelled', async () => {
    let posts = 0
    installFetch(fleetOf(MAIN_ROW, readyRow()), (u, opts) => {
      if (u.includes('/make-live') && isPost(opts)) { posts++; return res({ ok: true }) }
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    const menu = await openRowMenu()
    fireEvent.click(menu.getByText('Make live'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(posts).toBe(0)
  })
})

describe('DevFleetPage gateway restart handshake', () => {
  const origReload = window.location.reload

  beforeEach(() => { vi.restoreAllMocks() })
  afterEach(() => {
    Object.defineProperty(window.location, 'reload', { configurable: true, value: origReload })
  })

  it('reloads once a gateway answers when no identity was captured', async () => {
    const reloadSpy = vi.fn()
    Object.defineProperty(window.location, 'reload', { configurable: true, value: reloadSpy })
    installFetch({ ...fleetOf(MAIN_ROW), gateway_service_active: true }, (u, opts) => {
      if (u.includes('/restart-gateway') && isPost(opts)) return res({ ok: true })
      return null
    })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Restart gateway')).toBeInTheDocument(), { timeout: 4000 })
    fireEvent.click(screen.getByLabelText('Restart gateway'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restart' }))
    // No start_id in the response -> legacy degrade: any answer means "back".
    await waitFor(() => expect(reloadSpy).toHaveBeenCalled(), { timeout: 12000 })
  }, 20000)

  it('reports a refused restart in the persistent banner', async () => {
    installFetch({ ...fleetOf(MAIN_ROW), gateway_service_active: true }, (u, opts) => {
      if (u.includes('/restart-gateway') && isPost(opts)) return res({ ok: false, error: 'systemctl: unit not loaded' })
      return null
    })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Restart gateway')).toBeInTheDocument(), { timeout: 4000 })
    fireEvent.click(screen.getByLabelText('Restart gateway'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restart' }))
    const banner = await waitFor(() => screen.getByTestId('gateway-restart-error'))
    expect(banner).toHaveTextContent('systemctl: unit not loaded')
    // Dismissing the banner is the user's job, so it must survive until then.
    fireEvent.click(within(banner).getByRole('button'))
    await waitFor(() => expect(screen.queryByTestId('gateway-restart-error')).toBeNull())
  }, 15000)

  it('names the failed operation when the restart POST cannot be sent', async () => {
    installFetch({ ...fleetOf(MAIN_ROW), gateway_service_active: true }, (u, opts) => {
      if (u.includes('/restart-gateway') && isPost(opts)) return Promise.reject(new Error('Failed to fetch'))
      return null
    })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Restart gateway')).toBeInTheDocument(), { timeout: 4000 })
    fireEvent.click(screen.getByLabelText('Restart gateway'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restart' }))
    const banner = await waitFor(() => screen.getByTestId('gateway-restart-error'))
    expect(banner).toHaveTextContent('Restart failed: Failed to fetch')
  }, 15000)

  it('does not bounce the gateway when the restart confirm is cancelled', async () => {
    let posts = 0
    installFetch({ ...fleetOf(MAIN_ROW), gateway_service_active: true }, (u, opts) => {
      if (u.includes('/restart-gateway') && isPost(opts)) { posts++; return res({ ok: true }) }
      return null
    })
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Restart gateway')).toBeInTheDocument(), { timeout: 4000 })
    fireEvent.click(screen.getByLabelText('Restart gateway'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(posts).toBe(0)
  })
})

describe('DevFleetPage post-action refresh', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('falls back to a plain invalidate when the forced rebuild returns no data', async () => {
    const urls: string[] = []
    installFetch(null, (u) => {
      if (u.includes('/fleet')) {
        urls.push(u)
        // The forced rebuild answers with no payload at all.
        return u.includes('fresh=1') ? res(null) : res(fleetOf(MAIN_ROW, readyRow()))
      }
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    const before = urls.length
    fireEvent.click(screen.getByLabelText('Refresh fleet'))
    await waitFor(() => expect(urls.some((u) => u.includes('fresh=1'))).toBe(true))
    // The empty forced answer must not become the rendered state: a plain
    // refetch follows and the rows survive.
    await waitFor(() => expect(urls.length).toBeGreaterThan(before + 1))
    expect(screen.getByText('wt-a')).toBeInTheDocument()
  })

  it('falls back to a plain invalidate when the forced rebuild fails outright', async () => {
    const urls: string[] = []
    installFetch(null, (u) => {
      if (u.includes('/fleet')) {
        urls.push(u)
        return u.includes('fresh=1')
          ? Promise.reject(new Error('rebuild failed'))
          : res(fleetOf(MAIN_ROW, readyRow()))
      }
      return null
    })
    renderPage()
    await waitForRow('wt-a')
    const before = urls.length
    fireEvent.click(screen.getByLabelText('Refresh fleet'))
    await waitFor(() => expect(urls.some((u) => u.includes('fresh=1'))).toBe(true))
    await waitFor(() => expect(urls.length).toBeGreaterThan(before + 1))
    expect(screen.getByText('wt-a')).toBeInTheDocument()
  })
})
