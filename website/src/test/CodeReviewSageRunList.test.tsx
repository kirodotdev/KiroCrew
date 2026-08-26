import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { Run } from '../apps/code-review-sage/lib/types'

import RunList from '../apps/code-review-sage/components/RunList'
import RunProgress from '../apps/code-review-sage/components/RunProgress'
import RunCard from '../apps/code-review-sage/components/RunCard'
import FailureNotice from '../apps/code-review-sage/components/FailureNotice'
import { failureReason } from '../apps/code-review-sage/lib/format'
import { typicalRunMs } from '../apps/code-review-sage/lib/format'

function makeRun(overrides: Partial<Run> = {}): Run {
  return {
    run_id: 'run-1',
    repo: 'acme/widgets',
    changes: ['https://github.com/acme/widgets/pull/7'],
    status: 'done',
    started_at: '2026-07-28T00:00:00Z',
    finished_at: '2026-07-28T00:05:00Z',
    summary: { report: { bands: { red: 2, yellow: 1, green: 3 } } },
    ...overrides,
  }
}

describe('RunList / RunCard', () => {
  const noop = () => {}

  it('renders a card per run with its identity and status', () => {
    render(
      <RunList
        runs={[makeRun(), makeRun({ run_id: 'run-2', repo: 'acme/gadgets', status: 'running', finished_at: undefined })]}
        loading={false}
        selectedRunId={null}
        onSelect={noop}
        onNewReview={noop}
      />,
    )
    expect(screen.getByRole('button', { name: /Review of acme\/widgets/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /acme\/gadgets/ })).toBeInTheDocument()
    // Distinct status treatment surfaces as the pill label.
    expect(screen.getByText('Done')).toBeInTheDocument()
    expect(screen.getByText('Running')).toBeInTheDocument()
    // Finished run shows its red / yellow band counts. The verb agrees with the
    // count: these go through i18next plural forms, where the old concatenated
    // label said "2 needs review" for every value.
    expect(screen.getByTitle('2 need review')).toBeInTheDocument()
    expect(screen.getByTitle('1 worth a glance')).toBeInTheDocument()
  })

  it('parses a PR identity and a "+N more" tail when there is no repo', () => {
    render(
      <RunList
        runs={[makeRun({
          repo: undefined,
          changes: ['https://github.com/acme/widgets/pull/7', 'https://github.com/acme/widgets/pull/8'],
        })]}
        loading={false}
        selectedRunId={null}
        onSelect={noop}
        onNewReview={noop}
      />,
    )
    expect(screen.getByText('acme/widgets#7')).toBeInTheDocument()
    expect(screen.getByText('+1 more')).toBeInTheDocument()
  })

  it('marks the selected card and fires onSelect with the run id', () => {
    const onSelect = vi.fn()
    render(
      <RunList
        runs={[makeRun(), makeRun({ run_id: 'run-2', repo: 'acme/gadgets' })]}
        loading={false}
        selectedRunId="run-1"
        onSelect={onSelect}
        onNewReview={noop}
      />,
    )
    const selected = screen.getByRole('button', { name: /Review of acme\/widgets/ })
    expect(selected).toHaveAttribute('aria-current', 'true')
    expect(selected.className).toContain('border-accent')

    const other = screen.getByRole('button', { name: /acme\/gadgets/ })
    expect(other).toHaveAttribute('aria-current', 'false')
    fireEvent.click(other)
    expect(onSelect).toHaveBeenCalledWith('run-2')
  })

  it('shows the loading skeleton (and no cards) while loading', () => {
    render(
      <RunList
        runs={[makeRun()]}
        loading
        selectedRunId={null}
        onSelect={noop}
        onNewReview={noop}
      />,
    )
    expect(screen.getByText('Loading reviews…')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Review of acme\/widgets/ })).not.toBeInTheDocument()
  })

  it('shows the empty state, with exactly ONE new-review action in the column', () => {
    // The empty state used to carry its own "Start a review" CTA, which put a
    // second button a few pixels below the column's own one. There must be
    // exactly one, and it must work.
    const onNewReview = vi.fn()
    render(
      <RunList
        runs={[]}
        loading={false}
        selectedRunId={null}
        onSelect={noop}
        onNewReview={onNewReview}
      />,
    )
    expect(screen.getByText('No reviews yet')).toBeInTheDocument()
    const actions = screen.getAllByRole('button', { name: /New review/ })
    expect(actions).toHaveLength(1)
    fireEvent.click(actions[0])
    expect(onNewReview).toHaveBeenCalledTimes(1)
  })

  it('renders the error line instead of cards', () => {
    render(
      <RunList
        runs={[makeRun()]}
        loading={false}
        error="Boom"
        selectedRunId={null}
        onSelect={noop}
        onNewReview={noop}
      />,
    )
    expect(screen.getByText('Boom')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Review of acme\/widgets/ })).not.toBeInTheDocument()
  })
})

describe('RunProgress', () => {
  it('computes the run-level progress bar from terminal-phase changes', () => {
    const run = makeRun({
      status: 'running',
      finished_at: undefined,
      changes: ['https://github.com/acme/widgets/pull/1', 'https://github.com/acme/widgets/pull/2'],
      change_ids: ['c1', 'c2'],
      progress: { c1: { phase: 'done' }, c2: { phase: 'reviewing' } },
    })
    render(<RunProgress run={run} />)
    const bar = screen.getByRole('progressbar')
    expect(bar).toHaveAttribute('aria-valuenow', '50')
    expect(bar).toHaveAttribute('aria-valuemin', '0')
    expect(bar).toHaveAttribute('aria-valuemax', '100')
    expect(screen.getByText('1 / 2 reviewed')).toBeInTheDocument()
    // Per-change phase labels render.
    expect(screen.getByText('Done')).toBeInTheDocument()
    expect(screen.getByText('Reviewing')).toBeInTheDocument()
  })

  it('shows a ticking elapsed clock while running', () => {
    const run = makeRun({
      status: 'running',
      finished_at: undefined,
      started_at: new Date(Date.now() - 65_000).toISOString(),
    })
    render(<RunProgress run={run} />)
    // ~1:05 elapsed — a m:ss clock is present.
    expect(screen.getByText(/^\d+:\d{2}$/)).toBeInTheDocument()
  })

  it('offers Cancel only while running and calls onCancel', () => {
    const onCancel = vi.fn()
    const { unmount } = render(
      <RunProgress run={makeRun({ status: 'running', finished_at: undefined })} onCancel={onCancel} />,
    )
    const btn = screen.getByRole('button', { name: /Cancel review/ })
    fireEvent.click(btn)
    expect(onCancel).toHaveBeenCalledTimes(1)
    unmount()

    // A finished run offers no Cancel button.
    render(<RunProgress run={makeRun({ status: 'done' })} onCancel={onCancel} />)
    expect(screen.queryByRole('button', { name: /Cancel/ })).not.toBeInTheDocument()
    expect(onCancel).toHaveBeenCalledTimes(1)
  })
})

describe('RunProgress — one signal, not six', () => {
  /** A live run over a single PR: the case that had the most duplication. */
  function oneChangeRunning() {
    return {
      run_id: 'r1',
      changes: ['https://github.com/acme/widgets/pull/711'],
      change_ids: ['GH-acme-widgets-711'],
      status: 'running' as const,
      started_at: new Date(Date.now() - 16_000).toISOString(),
      progress: { 'GH-acme-widgets-711': { phase: 'reviewing' } },
    }
  }

  it('does not repeat the PR as a per-change row when there is only one', () => {
    // The row named the same PR as the pane header and restated the bar.
    render(<RunProgress run={oneChangeRunning()} pool={{ busy: 1, max: 5 }} />)
    expect(screen.getByRole('progressbar')).toBeInTheDocument()
    expect(screen.queryByRole('list')).toBeNull()
  })

  it('hides pool utilisation when it only restates the progress bar', () => {
    render(<RunProgress run={oneChangeRunning()} pool={{ busy: 1, max: 5 }} />)
    expect(screen.queryByText(/reviewers busy/i)).toBeNull()
  })

  it('shows pool utilisation once it carries information', () => {
    // Several PRs in one run, or other runs competing for the same workers.
    const run = {
      ...oneChangeRunning(),
      changes: ['https://github.com/a/b/pull/1', 'https://github.com/a/b/pull/2'],
      change_ids: ['GH-a-b-1', 'GH-a-b-2'],
    }
    render(<RunProgress run={run} pool={{ busy: 2, max: 5 }} />)
    expect(screen.getByText(/2 of 5 reviewers busy/i)).toBeInTheDocument()
    // With more than one change the per-change rows earn their place again.
    expect(screen.getByRole('list')).toBeInTheDocument()
  })

  it('states the cooperative-cancel caveat exactly once', () => {
    // It used to be both a tooltip and a visible line.
    render(<RunProgress run={oneChangeRunning()} onCancel={() => {}} />)
    const cancel = screen.getByRole('button', { name: /Cancel review/i })
    expect(cancel.getAttribute('title')).toBeNull()
    expect(screen.getAllByText(/already being reviewed will finish/i)).toHaveLength(1)
  })
})

describe('a run whose every change failed', () => {
  // The backend now records such a run as "error", but runs recorded BEFORE it
  // did keep status "done" on disk — and a green Done beside "0 / 1 reviewed ·
  // 1 failed" is a contradiction the user has to resolve themselves.
  const allFailed = {
    run_id: 'run-fail',
    repo: 'acme/widgets',
    changes: ['https://github.com/acme/widgets/pull/7'],
    change_ids: ['GH-acme-widgets-7'],
    status: 'done' as const,
    started_at: new Date(Date.now() - 60_000).toISOString(),
    finished_at: new Date(Date.now() - 10_000).toISOString(),
    progress: {
      'GH-acme-widgets-7': {
        phase: 'failed',
        error: 'review produced no result record',
      },
    },
    summary: { ok: true, changes: 1, result_records: 0 },
  }

  it('reads as Error, not Done', () => {
    render(<RunCard run={allFailed} selected={false} onSelect={() => {}} />)
    expect(screen.getByText('Error')).toBeInTheDocument()
    expect(screen.queryByText('Done')).toBeNull()
  })

  it('does not paint a full accent bar, which would read as success', () => {
    const { container } = render(<RunProgress run={allFailed} />)
    const fill = container.querySelector('[role="progressbar"] > div')
    expect(fill?.className).toContain('bg-danger')
    expect(fill?.className).not.toContain('bg-accent')
  })

  it('counts the failure separately from reviewed', () => {
    render(<RunProgress run={allFailed} />)
    expect(screen.getByText(/0 \/ 1 reviewed/)).toBeInTheDocument()
    expect(screen.getByText(/1 failed/)).toBeInTheDocument()
  })

  it('still reads Done when a change actually succeeded', () => {
    render(<RunCard
      run={{
        ...allFailed,
        progress: { 'GH-acme-widgets-7': { phase: 'done' } },
      }}
      selected={false}
      onSelect={() => {}}
    />)
    expect(screen.getByText('Done')).toBeInTheDocument()
  })
})

describe('progress inside one opaque review turn', () => {
  const live = {
    run_id: 'run-live',
    repo: 'acme/widgets',
    changes: ['https://github.com/acme/widgets/pull/7'],
    change_ids: ['GH-acme-widgets-7'],
    status: 'running' as const,
    started_at: new Date(Date.now() - 33_000).toISOString(),
    progress: {
      'GH-acme-widgets-7': {
        phase: 'reviewing',
        activity: { tool: 'execute_bash', step: 14 },
      },
    },
  }

  it('shows what the reviewer is doing right now', () => {
    render(<RunProgress run={live} />)
    // Without this the pane sits at "0 / 1 reviewed" for minutes with no sign of
    // life, which is indistinguishable from stuck.
    expect(screen.getByText(/execute_bash/)).toBeInTheDocument()
    expect(screen.getByText(/step 14/)).toBeInTheDocument()
  })

  it('sweeps an indeterminate bar rather than showing an empty trough', () => {
    const { container } = render(<RunProgress run={live} />)
    const fill = container.querySelector('[role="progressbar"] > div')
    expect(fill?.className).toContain('animate-sage-sweep')
  })

  it('switches to a real percentage once something finishes', () => {
    const { container } = render(<RunProgress run={{
      ...live,
      changes: [...live.changes, 'https://github.com/acme/widgets/pull/8'],
      change_ids: [...live.change_ids, 'GH-acme-widgets-8'],
      progress: {
        'GH-acme-widgets-7': { phase: 'done' },
        'GH-acme-widgets-8': { phase: 'reviewing' },
      },
    }} />)
    const fill = container.querySelector('[role="progressbar"] > div') as HTMLElement
    expect(fill.className).not.toContain('animate-sage-sweep')
    expect(fill.style.width).toBe('50%')
  })

  it('shows how long this usually takes when history supports it', () => {
    render(<RunProgress run={live} typicalMs={7 * 60_000} />)
    expect(screen.getByText(/usually ~/)).toBeInTheDocument()
  })

  it('says nothing about duration when there is no history', () => {
    render(<RunProgress run={live} typicalMs={null} />)
    expect(screen.queryByText(/usually ~/)).toBeNull()
  })
})

describe('typicalRunMs', () => {
  const finished = (ms: number, changes = 1) => ({
    status: 'done' as const,
    changes: Array.from({ length: changes }, (_, i) => `https://x/pull/${i}`),
    started_at: new Date(1_000_000).toISOString(),
    finished_at: new Date(1_000_000 + ms).toISOString(),
  })

  it('needs more than one sample to make a claim', () => {
    expect(typicalRunMs([finished(60_000)], 1)).toBeNull()
  })

  it('takes the median so one timed-out run cannot dominate', () => {
    expect(typicalRunMs(
      [finished(60_000), finished(90_000), finished(3_600_000)], 1)).toBe(90_000)
  })

  it('only compares runs of the same size', () => {
    // Duration scales with the number of PRs, so a 10-PR run says nothing about
    // how long a single-PR review takes.
    expect(typicalRunMs([finished(60_000, 10), finished(90_000, 10)], 1)).toBeNull()
  })

  it('ignores runs that never finished', () => {
    expect(typicalRunMs([
      { status: 'running' as const, changes: ['a'], started_at: new Date(0).toISOString() },
      finished(60_000),
    ], 1)).toBeNull()
  })
})

describe('why a review failed', () => {
  const failed = (error: string, over: Record<string, unknown> = {}) => ({
    run_id: 'run-x',
    repo: 'acme/widgets',
    changes: ['https://github.com/acme/widgets/pull/7'],
    change_ids: ['GH-acme-widgets-7'],
    status: 'error' as const,
    started_at: new Date(Date.now() - 600_000).toISOString(),
    finished_at: new Date(Date.now() - 10_000).toISOString(),
    error,
    progress: { 'GH-acme-widgets-7': { phase: 'failed', error } },
    ...over,
  })

  it('explains a killed reviewer instead of quoting the driver', () => {
    // The most common cause, and not the pull request's fault: restarting the
    // gateway takes the reviewer process down with it.
    const reason = failureReason(failed('Runtime process died during prompt'))
    expect(reason?.text).toMatch(/reviewer process stopped/i)
    expect(reason?.text).toMatch(/gateway restarting/i)
    // The driver's own wording is kept for a bug report.
    expect(reason?.raw).toBe('Runtime process died during prompt')
  })

  it('explains a review that recorded nothing', () => {
    expect(failureReason(failed('review produced no result record'))?.text)
      .toMatch(/recorded no findings/i)
  })

  it('explains a timeout', () => {
    expect(failureReason(failed('review turn timed out'))?.text)
      .toMatch(/past its time limit/i)
  })

  it('passes an unrecognised cause through verbatim', () => {
    // Better a raw message than a generic one that hides what happened.
    expect(failureReason(failed('gh: 502 from api.github.com'))?.text)
      .toBe('gh: 502 from api.github.com')
  })

  it('says nothing for a run that did not fail', () => {
    expect(failureReason({
      ...failed(''), status: 'done' as const,
      progress: { 'GH-acme-widgets-7': { phase: 'done' } },
    })).toBeNull()
  })

  it('prefers the named change cause over the run-level one', () => {
    // On a multi-PR run the run-level error may belong to a different change.
    const run = {
      ...failed('run level cause'),
      changes: ['https://github.com/acme/widgets/pull/7',
        'https://github.com/acme/widgets/pull/8'],
      change_ids: ['GH-acme-widgets-7', 'GH-acme-widgets-8'],
      progress: {
        'GH-acme-widgets-7': { phase: 'done' },
        'GH-acme-widgets-8': { phase: 'failed', error: 'this change timed out' },
      },
    }
    expect(failureReason(run, 'GH-acme-widgets-8')?.text)
      .toMatch(/past its time limit/i)
  })

  it('shows the reason on the failed card', () => {
    render(<RunCard
      run={failed('Runtime process died during prompt')}
      selected={false}
      onSelect={() => {}}
    />)
    expect(screen.getByText(/reviewer process stopped/i)).toBeInTheDocument()
  })

  it('offers to run it again', () => {
    const onRetry = vi.fn()
    render(<FailureNotice
      run={failed('Runtime process died during prompt')}
      onRetry={onRetry}
    />)
    fireEvent.click(screen.getByRole('button', { name: /Run it again/ }))
    expect(onRetry).toHaveBeenCalled()
  })

  it('renders nothing when the run succeeded', () => {
    const { container } = render(<FailureNotice run={{
      ...failed(''), status: 'done' as const,
      progress: { 'GH-acme-widgets-7': { phase: 'done' } },
    }} />)
    expect(container).toBeEmptyDOMElement()
  })
})
