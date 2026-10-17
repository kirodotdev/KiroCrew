/**
 * Isolated capture entry for the apps-1 batch of the error-state sweep.
 *
 * WHY ISOLATED: the code-review-sage failure surfaces only appear once a review
 * has been started and failed against a live GitHub session; booting the full
 * SPA for that needs the app shell, a gateway and `gh` credentials. The
 * components below are prop-driven, so the failed states can be rendered
 * directly with the exact values the app would hand them.
 *
 * Two columns per frame:
 *   BEFORE — the hand-written surfaces reconstructed verbatim from origin/main
 *            (a bare `text-danger` div, a `border-danger` box re-implementing
 *            the notice visuals, an error hidden in a `title=` tooltip).
 *   AFTER  — the real components from this branch, rendering through the shared
 *            `ErrorNotice` with its agent hand-off where the surface holds no
 *            draft, and with the `No hand-off` decision where it does.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import type { ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'

// `../src/i18n/all` registers every language catalog (plain `../src/i18n` is
// English-only), as the shared entry contract requires of every capture.
import { initI18n } from '../src/i18n/all'
import ErrorNotice from '../src/components/ErrorNotice'
import FailureNotice from '../src/apps/code-review-sage/components/FailureNotice'
import RunList from '../src/apps/code-review-sage/components/RunList'
import type { Run } from '../src/apps/code-review-sage/lib/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const FAILED_RUN = {
  run_id: 'run-x',
  repo: 'acme/widgets',
  changes: ['https://github.com/acme/widgets/pull/7'],
  change_ids: ['GH-acme-widgets-7'],
  status: 'error',
  started_at: new Date(Date.now() - 600_000).toISOString(),
  finished_at: new Date(Date.now() - 10_000).toISOString(),
  error: 'Runtime process died during prompt',
  progress: { 'GH-acme-widgets-7': { phase: 'failed', error: 'Runtime process died during prompt' } },
} as unknown as Run

const LIST_ERROR = 'Could not reach the reviewer backend (HTTP 502).'
const LINKS_ERROR = 'https://example.com/not-a-pr is not a pull request link.'
const START_ERROR = "Couldn't start"

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="m-0 text-[11px] font-semibold uppercase tracking-wider text-muted">{title}</h3>
      {children}
    </section>
  )
}

function Column({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex-1 min-w-0 flex flex-col gap-6 rounded-xl border border-border bg-card p-4">
      <div className="text-[12px] font-semibold text-text">{label}</div>
      {children}
    </div>
  )
}

/** The origin/main shapes, reconstructed so the diff is visible side by side. */
function Before() {
  return (
    <Column label="Before (origin/main)">
      <Section title="FailureNotice — bespoke box, no hand-off">
        <div className="rounded-lg border border-danger/50 bg-danger-subtle px-3.5 py-2.5">
          <div className="text-[12.5px] font-medium text-danger">This review failed</div>
          <div className="mt-0.5 text-[12.5px] text-text leading-[1.6]">
            The reviewer process stopped before it finished — usually the gateway restarted mid-review.
          </div>
          <div className="mt-1 font-mono text-[11px] text-muted break-words">Runtime process died during prompt</div>
        </div>
      </Section>
      <Section title="RunList — bare text-danger div">
        <div className="px-1 py-2 text-[13px] text-danger">{LIST_ERROR}</div>
      </Section>
      <Section title="PrPickList paste-links — bare text-danger div">
        <div className="text-[11.5px] text-danger">{LINKS_ERROR}</div>
      </Section>
      <Section title="AgentSessionButton — cause hidden in title=">
        <span className="text-[10.5px] text-danger" title="Session could not be created: slot limit reached">
          {START_ERROR}
        </span>
      </Section>
    </Column>
  )
}

function After() {
  return (
    <Column label="After (this branch)">
      <Section title="FailureNotice — ErrorNotice + askAgent, retry beside it">
        <FailureNotice run={FAILED_RUN} onRetry={() => {}} />
      </Section>
      <Section title="RunList — ErrorNotice askAgent (list failure, nothing to lose)">
        <div style={{ height: 72, position: 'relative' }}>
          <RunList runs={[]} loading={false} error={LIST_ERROR} selectedRunId={null} onSelect={() => {}} />
        </div>
      </Section>
      <Section title="PrPickList paste-links — ErrorNotice, No hand-off (textarea draft)">
        <ErrorNotice message={LINKS_ERROR} variant="inline" />
      </Section>
      <Section title="AgentSessionButton — cause visible, askAgent">
        <ErrorNotice
          title={START_ERROR}
          message="Session could not be created: slot limit reached"
          variant="inline"
          askAgent
        />
      </Section>
    </Column>
  )
}

async function main() {
  await initI18n('en')
  const root = createRoot(document.getElementById('root')!)
  root.render(
    <MemoryRouter>
      <div className="min-h-screen bg-bg text-text p-6" data-testid="scene">
        <div className="flex gap-4 items-start" style={{ maxWidth: 1080 }}>
          <Before />
          <After />
        </div>
      </div>
    </MemoryRouter>,
  )
}

void main()
