// What these pin, and why a React `key` needs a test at all.
//
// Both pipeline views hold drill-down state and neither keys it on the
// repository. As a standalone app that was safe: each resolved its repository
// once and never saw it change. Inside Issue Radar it changes under them, and the
// L2 session lookup is keyed on the issue NUMBER — so an item expanded under one
// repository, left open across a switch, would show the OTHER repository's
// sessions and credit costs under the same number. The host remounts them
// instead, which discards that state.
//
// A React `key` is not observable from the DOM, so asserting on it directly would
// pin an implementation detail while proving nothing. These tests assert the
// BEHAVIOUR instead: they count MOUNTS of stubbed children and check that state a
// child holds is actually gone. That is also what makes the last test meaningful —
// it proves the remount is narrow, not a re-render sledgehammer.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import * as React from 'react'
import type { RepoRef } from '../api'

const mounts: string[] = []

let active = { owner: 'acme', repo: 'alpha' } as {
  owner: string
  repo: string
  provider?: string
  host?: string
}

// The CONNECTED records. `active` can be slug-only, so this is where the forge
// half of the identity has to come from.
let repos: Array<{ owner: string; repo: string; provider?: string; host?: string }> = [
  { owner: 'acme', repo: 'alpha' },
]

vi.mock('../context', () => ({
  useIssueRadar: () => ({ active, repos }),
}))

// Each stub records its mounts and owns a scrap of state, standing in for the
// real views' open step / open item.
function makeStub(name: string) {
  return function Stub({ repo }: { repo: RepoRef }) {
    const [open, setOpen] = React.useState(false)
    React.useEffect(() => {
      mounts.push(`${name}:${repo.owner}/${repo.repo}`)
      // Re-running this on a repo change would append without a remount, and a remount
      // would stop being distinguishable from a plain re-render.
      // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-once on purpose: this log IS the mount counter every assertion in this file reads
    }, [])
    return (
      <div>
        <span data-testid={`${name}-repo`}>{`${repo.owner}/${repo.repo}`}</span>
        <span data-testid={`${name}-identity`}>
          {`${repo.provider ?? '-'}|${repo.host ?? '-'}`}
        </span>
        <span data-testid={`${name}-open`}>{open ? 'open' : 'closed'}</span>
        <button data-testid={`${name}-expand`} onClick={() => setOpen(true)}>
          expand
        </button>
      </div>
    )
  }
}

vi.mock('../pipeline/views/GlobalPipelineView', () => ({ default: makeStub('fold') }))
vi.mock('../pipeline/views/PipelineView', () => ({ default: makeStub('lanes') }))

const { default: PipelineDashboardView } = await import('./PipelineDashboardView')

beforeEach(() => {
  mounts.length = 0
  active = { owner: 'acme', repo: 'alpha' }
  repos = [{ owner: 'acme', repo: 'alpha' }]
})

describe('PipelineDashboardView — the repository is handed in, and a switch discards state', () => {
  /** Reveal the lanes board, which lives behind the second tab. */
  function openLanes(): void {
    // `mouseDown`, not `click`: Radix's tab trigger activates on mousedown (so a
    // drag off the rail cannot leave the selection half-applied), and
    // `fireEvent.click` does not synthesise the preceding mousedown.
    fireEvent.mouseDown(screen.getByRole('tab', { name: /lanes/i }))
  }

  it('keeps the two boards TABBED rather than stacking them', () => {
    // A regression this relocation introduced and had to undo. The page being
    // replaced tabbed them deliberately -- they answer different questions from
    // different data, so one page implies their numbers are comparable -- and an
    // earlier draft of the move stacked them, which also had both polling at once.
    // Pinned on the ROLE, not on a class, because the accessibility contract is the
    // point: the row is the shared Radix tabs precisely because a hand-rolled
    // tablist once announced arrow-key navigation it did not implement.
    render(<PipelineDashboardView />)
    expect(screen.getAllByRole('tab')).toHaveLength(2)
    // Only the selected board is mounted, so a reader looking at one is not paying
    // for the other's polling.
    expect(screen.getByTestId('fold-repo')).toBeTruthy()
    expect(screen.queryByTestId('lanes-repo')).toBeNull()
  })

  it("hands Issue Radar's active repository to both views", () => {
    render(<PipelineDashboardView />)
    expect(screen.getByTestId('fold-repo').textContent).toBe('acme/alpha')
    openLanes()
    expect(screen.getByTestId('lanes-repo').textContent).toBe('acme/alpha')
  })

  it('REMOUNTS the fold view on a repository switch, discarding its drill-down', () => {
    const { rerender } = render(<PipelineDashboardView />)
    fireEvent.click(screen.getByTestId('fold-expand'))
    expect(screen.getByTestId('fold-open').textContent).toBe('open')

    active = { owner: 'acme', repo: 'beta' }
    rerender(<PipelineDashboardView />)

    // The whole point: the item is closed, not carried across.
    expect(screen.getByTestId('fold-open').textContent).toBe('closed')
    expect(mounts).toContain('fold:acme/alpha')
    expect(mounts).toContain('fold:acme/beta')
  })

  it('remounts the lanes view on the same switch', () => {
    const { rerender } = render(<PipelineDashboardView />)
    openLanes()
    fireEvent.click(screen.getByTestId('lanes-expand'))
    expect(screen.getByTestId('lanes-open').textContent).toBe('open')

    active = { owner: 'acme', repo: 'beta' }
    rerender(<PipelineDashboardView />)
    expect(screen.getByTestId('lanes-open').textContent).toBe('closed')
  })

  it('does NOT remount when nothing about the repository changed', () => {
    // The context hands out a fresh object on unrelated updates (a poll tick, an
    // issue refresh). The key is a STRING derived from the fields, so those
    // updates produce the same key and the operator's open item survives. Worth
    // pinning because the obvious alternative -- keying on the context object --
    // would remount on every tick; note this guards the KEY's stability, not the
    // `useMemo`, which is a re-render optimisation and cannot affect remounting.
    const { rerender } = render(<PipelineDashboardView />)
    fireEvent.click(screen.getByTestId('fold-expand'))
    expect(screen.getByTestId('fold-open').textContent).toBe('open')

    active = { ...active }
    rerender(<PipelineDashboardView />)
    expect(screen.getByTestId('fold-open').textContent).toBe('open')
    expect(mounts.filter((m) => m.startsWith('fold:')).length).toBe(1)
  })

  it('survives many unrelated context updates, not just one', () => {
    // The single-update case above passes even against a key built from the
    // context OBJECT if React happens to reuse it. Ten fresh objects in a row is
    // what a real polling session looks like, and a key that is not derived from
    // the fields loses the open item on the first of them.
    const { rerender } = render(<PipelineDashboardView />)
    fireEvent.click(screen.getByTestId('fold-expand'))
    for (let i = 0; i < 10; i++) {
      active = { ...active }
      rerender(<PipelineDashboardView />)
    }
    expect(screen.getByTestId('fold-open').textContent).toBe('open')
    expect(mounts.filter((m) => m.startsWith('fold:')).length).toBe(1)
  })
})

describe('PipelineDashboardView — the identity handed down names the forge', () => {
  it('completes a slug-only active repo from the CONNECTED record', () => {
    // The host hands down `{owner, repo}` with no forge when nothing is stored or
    // the stored repository is no longer connected. Passing that on would omit
    // `provider`, which the backend reads as public GitHub -- so this GitLab
    // repository would be served GitHub's trail, issue cache and queue shard under
    // its own heading, with nothing on screen marking the substitution. The
    // backend's refusal cannot fire on a request that never names a forge.
    active = { owner: 'group', repo: 'thing' }
    repos = [{ owner: 'group', repo: 'thing', provider: 'gitlab', host: 'gitlab.example.com' }]
    render(<PipelineDashboardView />)
    expect(screen.getByTestId('fold-identity').textContent).toBe('gitlab|gitlab.example.com')
  })

  it('leaves a GitHub repository with no forge fields to state', () => {
    // Absent means public GitHub on both sides, so nothing is invented here: the
    // common case must not start sending a provider it never had.
    active = { owner: 'acme', repo: 'alpha' }
    repos = [{ owner: 'acme', repo: 'alpha' }]
    render(<PipelineDashboardView />)
    expect(screen.getByTestId('fold-identity').textContent).toBe('-|-')
  })

  it('prefers what the active repo states over the connected record', () => {
    // An explicit selection wins; the record only FILLS what is missing.
    active = { owner: 'group', repo: 'thing', provider: 'azure' }
    repos = [{ owner: 'group', repo: 'thing', provider: 'gitlab' }]
    render(<PipelineDashboardView />)
    expect(screen.getByTestId('fold-identity').textContent).toBe('azure|-')
  })

  it('does not invent a forge when the active repo is in no record', () => {
    // A mismatch is the host's problem, not a licence to guess: naming the wrong
    // forge is worse than naming none, because the backend would then refuse or
    // serve the wrong repository with equal confidence.
    active = { owner: 'ghost', repo: 'missing' }
    repos = [{ owner: 'acme', repo: 'alpha', provider: 'gitlab' }]
    render(<PipelineDashboardView />)
    expect(screen.getByTestId('fold-identity').textContent).toBe('-|-')
  })
})
