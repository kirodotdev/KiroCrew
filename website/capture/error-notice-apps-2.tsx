/**
 * Isolated capture entry for the apps-2 batch of the error-state sweep
 * (issue-radar detail / settings surfaces, md-notebook, meetings).
 *
 * WHY ISOLATED: these failure states only appear once a repo is connected and a
 * request against the provider fails; booting the full SPA for that needs the
 * app shell, a gateway and a forge session. The components below are
 * prop-driven, so the failed states render directly with the exact values the
 * app would hand them; the hook-driven views (CrewPageView, PrActionsBar,
 * MeetingsPage) are shown as the exact `ErrorNotice` call this branch makes.
 *
 * Two columns per frame:
 *   BEFORE — the hand-written surfaces reconstructed verbatim from origin/main
 *            (bare `text-danger` divs, an error dressed as an EmptyState, a
 *            cause hidden in a `title=` tooltip).
 *   AFTER  — the real components from this branch, rendering through the shared
 *            `ErrorNotice` with its agent hand-off where the surface holds no
 *            draft, and with the `No hand-off` decision where it does.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import type { ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { AlertTriangle, Inbox } from 'lucide-react'

// `../src/i18n/all` registers every language catalog (plain `../src/i18n` is
// English-only), as the shared entry contract requires of every capture.
import { initI18n } from '../src/i18n/all'
import ErrorNotice from '../src/components/ErrorNotice'
import { Btn, EmptyState } from '../src/components/ui'
import AiSummaryCard from '../src/apps/issue-radar/components/AiSummaryCard'
import LabelPicker from '../src/apps/issue-radar/components/LabelPicker'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SUMMARY_ERROR = new Error('HTTP 503: model backend unavailable')
const LABELS_ERROR = new Error('HTTP 403: resource not accessible by integration')
const CREW_LOAD_ERROR = 'HTTP 404: crew "tucana" not found'
const ACTION_ERROR = 'HTTP 422: review cannot be requested from the author'
const SYNC_ERROR = 'Calendar sync failed: token expired'

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
      <Section title="AiSummaryCard — bare text-danger span + retry">
        <span className="text-[13px] text-danger">
          Couldn't generate a summary.{' '}
          <button className="underline cursor-pointer bg-transparent text-danger">Retry</button>
        </span>
      </Section>
      <Section title="LabelPicker — bare text-danger div">
        <div className="text-[12px] text-danger py-2">{LABELS_ERROR.message}</div>
      </Section>
      <Section title="CrewPageView — load failure dressed as an EmptyState">
        <EmptyState icon={<Inbox className="lucide-inline" />} title="Couldn't load this crew" subtitle={CREW_LOAD_ERROR} />
      </Section>
      <Section title="PrActionsBar — cause hidden in title=">
        <div className="flex items-center gap-1.5 flex-wrap">
          <Btn danger title={ACTION_ERROR} aria-label="Dismiss error">
            <AlertTriangle className="lucide-inline" />
            Action failed
          </Btn>
        </div>
      </Section>
      <Section title="MeetingsPage — sync failure was toast-only (nothing in-page)">
        <div className="text-[12px] text-muted italic">(no in-page surface; the toast fades)</div>
      </Section>
    </Column>
  )
}

function After() {
  return (
    <Column label="After (this branch)">
      <Section title="AiSummaryCard — ErrorNotice askAgent, retry beside it">
        <AiSummaryCard summary="" fromCache={false} loading={false} fetching={false} error={SUMMARY_ERROR} onRegenerate={() => {}} />
      </Section>
      <Section title="LabelPicker — ErrorNotice, No hand-off (repo settings form)">
        <LabelPicker labels={[]} selected={[]} onToggle={() => {}} error={LABELS_ERROR} />
      </Section>
      <Section title="CrewPageView — ErrorNotice askAgent (read failure, no draft)">
        <ErrorNotice title="Couldn't load this crew" message={CREW_LOAD_ERROR} askAgent />
      </Section>
      <Section title="PrActionsBar — cause visible, askAgent, dismissable, own row above the buttons">
        <div className="w-full min-w-0">
          <ErrorNotice title="Action failed" message={ACTION_ERROR} askAgent onDismiss={() => {}} className="mb-1.5" />
          <div className="flex items-center gap-1.5 flex-wrap">
            <Btn>Approve</Btn>
            <Btn>Request changes</Btn>
          </div>
        </div>
      </Section>
      <Section title="MeetingsPage — in-page ErrorNotice askAgent (toast kept as transient feedback)">
        <ErrorNotice message={SYNC_ERROR} askAgent onDismiss={() => {}} />
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
