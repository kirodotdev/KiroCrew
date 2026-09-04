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

  // ── The discriminated causes from #7233 ──
  // Every fixture below is the string the backend really emits, copied from its
  // source so a reader can check it: the driver's per-change wording
  // (sage_lib/review_driver.py), the preflight's two answers
  // (sage_lib/review_pool.py::runtime_preflight) and the run-level sentences
  // routes.py::_first_change_error maps each reason to.
  it('tells a record that stopped short apart from one that was never written', () => {
    // The driver's per-change wording, and the run-level sentence for the same
    // reason. Both must reach the incomplete-record explanation, and NEITHER may
    // fall into the "no result record" branch — telling these apart is the whole
    // point of the backend change.
    for (const raw of [
      'review wrote a result record but never completed the review',
      'the reviewer wrote a findings record but never completed the review',
      'review_record_incomplete',
    ]) {
      const reason = failureReason(failed(raw))
      expect(reason?.text, raw).toMatch(/stopped before completing the review/i)
      expect(reason?.text, raw).not.toMatch(/recorded no findings/i)
      expect(reason?.raw, raw).toBe(raw)
    }
  })

  it('explains a host with no kiro-cli, naming what to install', () => {
    const reason = failureReason(failed(
      'the reviewer cannot run: no kiro-cli executable was found on this host '
      + '(the reviewer session is driven by kiro-cli — install it or add it to PATH)'))
    expect(reason?.text).toMatch(/never started/i)
    expect(reason?.text).toMatch(/PATH/)
    expect(reason?.raw).toMatch(/no kiro-cli executable/)
  })

  it('explains an install whose agent runtime will not load', () => {
    // A different remedy from the missing executable — the CLI may well be
    // installed — so it must not share that sentence.
    const reason = failureReason(failed(
      'the reviewer cannot run: the ACP runtime (kiro_crew.acp.runtime) is not '
      + 'importable in this install'))
    expect(reason?.text).toMatch(/cannot load the agent runtime/i)
    expect(reason?.text).not.toMatch(/PATH/)
  })

  it('explains the run-level card for a preflight-failed run', () => {
    // `_first_change_error` renders this when the per-change record kept only the
    // reason. It says "runtime is unavailable", never the enum, so a translator
    // keyed on `runtime_unavailable` alone would show it verbatim.
    for (const raw of [
      'the reviewer never ran: its agent runtime is unavailable on this host',
      'runtime_unavailable',
    ]) {
      expect(failureReason(failed(raw))?.text, raw)
        .toMatch(/agent runtime is unavailable on this host/i)
    }
  })

  it('leaves a missing agent spec with its own repair command', () => {
    // This message names kiro-cli too, and it already tells the reader exactly
    // what to run. A branch keyed on the bare word `kiro-cli` would replace it
    // with "install kiro-cli", so it is the regression guard for the narrower
    // pattern rather than an incidental pass-through case.
    const raw = "Agent spec 'code-review-sage-reviewer' is not installed: kiro-cli "
      + "found no 'code-review-sage-reviewer.json' in /home/u/.kiro/agents. Every "
      + 'turn fails until it is restored — repair with `kirocrew setup '
      + '--agent-only --clean`, then restart the gateway.'
    expect(failureReason(failed(raw))?.text).toBe(raw)
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

  // ── The cause TOKEN, so a reworded backend message still translates ──
  // Every branch above matches backend PROSE. That is the defect #7688 names:
  // reword any of those sentences and the regex stops matching, the card silently
  // reverts to untranslated pass-through, and nothing goes red because the
  // fixtures here are this file's own copies of the backend's strings. The
  // payload now carries the cause as a token beside the sentence, and the
  // translator falls back to it exactly where it used to give up.

  it('translates every cause with NO token at all, forever', () => {
    // THE acceptance criterion, and the reason the token is a fallback rather
    // than a replacement. `progress` lives inside a PERSISTED run record, so
    // every run already on disk has no token and never will -- the prose path is
    // the permanent compatibility path, not a transitional one. A fix that works
    // for new runs and degrades old ones would be worse than the bug.
    //
    // Table-driven over the real backend strings so this fails if any prose
    // branch is ever removed in favour of its token.
    for (const [raw, expected] of [
      ['Runtime process died during prompt', /stopped mid-review/i],
      ['the reviewer cannot run: no kiro-cli executable was found on this host',
        /never started/i],
      ['the reviewer cannot run: the ACP runtime (kiro_crew.acp.runtime) is not '
        + 'importable in this install', /cannot load the agent runtime/i],
      ['the reviewer never ran: its agent runtime is unavailable on this host',
        /agent runtime is unavailable on this host/i],
      ['review produced no result record', /recorded no findings/i],
      ['review wrote a result record but never completed the review',
        /stopped before completing the review/i],
      ['the reviewer wrote a findings record but never completed the review',
        /stopped before completing the review/i],
      ['review turn timed out', /past its time limit/i],
    ] as const) {
      const run = failed(raw)
      // Belt and braces: assert the fixture really carries no token, so this
      // cannot silently become a token test if `failed` ever grows one.
      expect('reason' in run, raw).toBe(false)
      expect(run.progress['GH-acme-widgets-7'], raw).not.toHaveProperty('reason')
      expect(failureReason(run)?.text, raw).toMatch(expected)
    }
  })

  it('translates the run-level no-record sentence, which prose alone misses', () => {
    // Evidence that the drift this change fixes is not hypothetical: it has
    // ALREADY happened, and is on main right now.
    //
    // `routes.py::_first_change_error` maps `no_review_recorded` to "the reviewer
    // finished but wrote no findings record". No prose branch matches it -- the
    // no-record branch keys on "no result record", which is the DRIVER's
    // per-change wording, a different sentence for the same cause. Compare the
    // sibling: the incomplete-record branch deliberately covers both its
    // wordings (see the comment on it), so the gap here is an asymmetry, not a
    // design choice.
    const sentence = 'the reviewer finished but wrote no findings record'
    // Untranslated without a token, exactly as on main. Asserted rather than
    // left implicit so this test states the before and the after.
    expect(failureReason(failed(sentence))?.text).toBe(sentence)
    // With the token the card explains the cause instead of quoting the backend.
    expect(failureReason(withToken(sentence, 'no_review_recorded'))?.text)
      .toMatch(/recorded no findings/i)
  })

  /** A failed run whose payload carries the token as well as the prose. */
  const withToken = (error: string, reason: string) => ({
    ...failed(error),
    reason,
    progress: { 'GH-acme-widgets-7': { phase: 'failed', error, reason } },
  })

  it('translates a REWORDED backend message by its token', () => {
    // The regression this closes, stated as the test: none of these sentences
    // matches any prose branch -- they are what a reword produces -- so before
    // the token each rendered as raw English.
    for (const [reworded, reason, expected] of [
      ['the reviewer stopped before it finished', 'review_record_incomplete',
        /stopped before completing the review/i],
      ['the reviewer left nothing behind', 'no_review_recorded',
        /recorded no findings/i],
      ['the review host cannot start a reviewer', 'runtime_unavailable',
        /agent runtime is unavailable on this host/i],
    ] as const) {
      const reason_ = failureReason(withToken(reworded, reason))
      expect(reason_?.text, reworded).toMatch(expected)
      // The raw backend text is still carried for the detail notice.
      expect(reason_?.raw, reworded).toBe(reworded)
    }
  })

  it('reads the token from the named change, like the prose', () => {
    // The token must resolve through the SAME per-change-then-run precedence the
    // sentence does, or a multi-PR run labels one change with another's cause.
    const run = {
      ...failed('run level cause'),
      reason: 'no_review_recorded',
      changes: ['https://github.com/acme/widgets/pull/7',
        'https://github.com/acme/widgets/pull/8'],
      change_ids: ['GH-acme-widgets-7', 'GH-acme-widgets-8'],
      progress: {
        'GH-acme-widgets-7': { phase: 'done' },
        'GH-acme-widgets-8': {
          phase: 'failed', error: 'this one stopped early',
          reason: 'review_record_incomplete',
        },
      },
    }
    expect(failureReason(run, 'GH-acme-widgets-8')?.text)
      .toMatch(/stopped before completing the review/i)
  })

  it('lets a matching prose branch win over the token', () => {
    // Not a style preference -- an information one. All three runtime-preflight
    // messages carry the single token `runtime_unavailable`, so a token-FIRST
    // lookup would replace this specific message (and the PATH remedy in it)
    // with the generic "runtime is unavailable" sentence.
    const reason = failureReason(withToken(
      'the reviewer cannot run: no kiro-cli executable was found on this host '
      + '(the reviewer session is driven by kiro-cli - install it or add it to PATH)',
      'runtime_unavailable'))
    expect(reason?.text).toMatch(/never started/i)
    expect(reason?.text).toMatch(/PATH/)
    expect(reason?.text).not.toMatch(/agent runtime is unavailable on this host/i)
  })

  it('keeps a missing agent spec verbatim even though it carries a token', () => {
    // This arrives on the failed-dispatch path, so it now carries the token
    // `review_failed` -- and it must STILL pass through, because it already tells
    // the reader exactly what to run. `review_failed` is deliberately absent from
    // the token table for this reason: it labels arbitrary spawn output, so the
    // prose is the information and the token is not a substitute for it.
    const raw = "Agent spec 'code-review-sage-reviewer' is not installed: kiro-cli "
      + "found no 'code-review-sage-reviewer.json' in /home/u/.kiro/agents. Every "
      + 'turn fails until it is restored - repair with `kirocrew setup '
      + '--agent-only --clean`, then restart the gateway.'
    expect(failureReason(withToken(raw, 'review_failed'))?.text).toBe(raw)
  })

  it('still passes an unrecognised cause through when no token came with it', () => {
    // A run recorded before the backend carried the token, which is every run
    // already on disk. The prose path must be unchanged for it.
    expect(failureReason(failed('gh: 502 from api.github.com'))?.text)
      .toBe('gh: 502 from api.github.com')
    expect(failureReason(withToken('gh: 502 from api.github.com', ''))?.text)
      .toBe('gh: 502 from api.github.com')
  })

  it('ignores a token it does not know', () => {
    // A backend that grows a fifth reason must not blank the card; the prose it
    // came with is still the best available answer.
    expect(failureReason(withToken('something new went wrong', 'brand_new_reason'))?.text)
      .toBe('something new went wrong')
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
