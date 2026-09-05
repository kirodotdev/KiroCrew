/**
 * Isolated capture entry for the pages-rest-2 batch of the error-state sweep
 * (top-level `src/pages/*` — Schedule, Webhooks, Projects, Logs, remote
 * artifact detail, session archive).
 *
 * WHY ISOLATED: every one of these pages is hook-driven (react-query polls,
 * `api.*` calls, redux), so its failed states only appear once a gateway
 * request actually fails. The surfaces are shown here as the exact
 * `ErrorNotice` calls this branch makes, with the same strings the pages pass.
 *
 * Two columns per frame:
 *   BEFORE — the hand-written surfaces reconstructed verbatim from origin/main
 *            (bare `text-danger` / `text-red-500` divs, a `<pre>` painted red,
 *            a page-local warn Banner, a Card with its own red heading, and a
 *            silent failure with nothing on screen at all).
 *   AFTER  — the shared `ErrorNotice`, with its agent hand-off where the surface
 *            holds no draft and the `No hand-off` decision where it does.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import type { ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { AlertTriangle } from 'lucide-react'

// `../src/i18n/all` registers every language catalog (plain `../src/i18n` is
// English-only), as the shared entry contract requires of every capture.
import { initI18n } from '../src/i18n/all'
import ErrorNotice from '../src/components/ErrorNotice'
import { Btn } from '../src/components/ui'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const LAST_ERROR = 'Traceback (most recent call last):\n  File "digest.py", line 41, in run\n    raise Skip("no new issues")\nkiro_crew.crons.Skip: no new issues'
const ROW_ACTION_ERROR = 'HTTP 409: job is already running'
const WEBHOOKS_LOAD_ERROR = 'HTTP 502: gateway unavailable'
const PLAN_ERROR = 'HTTP 422: spec has no runnable steps'
const LOG_LEVEL_ERROR = 'HTTP 403: log level is pinned by the service unit'
const REMOTE_ERROR = 'HTTP 404: artifact "weekly-digest" not found on provider'
const ARCHIVE_ERROR = 'HTTP 500: archive index unreadable'

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
      <Section title="SchedulePage — job dialog: last_error in a red <pre>">
        <div className="flex flex-col gap-1.5">
          <div className="text-[12px] text-muted font-medium">Last Error</div>
          <pre className="text-[12px] font-mono whitespace-pre-wrap break-words rounded border px-2.5 py-2 max-h-[200px] overflow-y-auto bg-danger/5 border-danger/20 text-danger">{LAST_ERROR}</pre>
        </div>
      </Section>
      <Section title="SchedulePage — row action failure: bare text-danger div">
        <div className="mt-1 text-danger text-[12px]">{ROW_ACTION_ERROR}</div>
      </Section>
      <Section title="WebhooksPage — load failure in a page-local warn Banner">
        <div className="flex items-start gap-2.5 px-4 py-3 rounded-lg border border-warn/40 bg-warn/10">
          <AlertTriangle size={16} className="text-warn shrink-0 mt-[1px]" />
          <div className="flex-1 min-w-0 text-[13px]">
            <div className="font-semibold text-text">Webhook settings are unavailable</div>
            <div className="text-muted">{WEBHOOKS_LOAD_ERROR} The reference below still describes the endpoint.</div>
          </div>
          <Btn>Retry</Btn>
        </div>
      </Section>
      <Section title="ProjectsPage — plan failure: hand-written red box">
        <div className="rounded-lg border border-danger/50 bg-danger/10 px-4 py-2.5 text-danger text-[13px]">{PLAN_ERROR}</div>
      </Section>
      <Section title="LogsPage — refused level change: silent (if r.ok, no else)">
        <div className="text-[12px] text-muted italic">(nothing on screen; the selector quietly keeps the old level)</div>
      </Section>
      <Section title="RemoteArtifactDetailPage — Card with its own red heading">
        <div className="rounded-xl border border-border bg-bg-elevated p-4">
          <div className="flex items-start gap-3">
            <AlertTriangle className="lucide-inline text-danger" />
            <div>
              <div className="text-sm text-danger font-medium">Failed to load remote artifact</div>
              <div className="text-[13px] text-muted mt-1">{REMOTE_ERROR}</div>
            </div>
          </div>
        </div>
      </Section>
      <Section title="SessionArchive — literal Tailwind red">
        <div className="text-red-500 text-[13px]">{ARCHIVE_ERROR}</div>
      </Section>
    </Column>
  )
}

function After() {
  return (
    <Column label="After (this branch)">
      <Section title="SchedulePage — ErrorNotice title 'Last Error', No hand-off (JobForm draft)">
        <ErrorNotice title="Last Error" message={LAST_ERROR} className="max-h-[200px] overflow-y-auto font-mono" />
      </Section>
      <Section title="SchedulePage — inline ErrorNotice askAgent (job is persisted)">
        <ErrorNotice variant="inline" className="mt-1 whitespace-normal" message={ROW_ACTION_ERROR} askAgent />
      </Section>
      <Section title="WebhooksPage — ErrorNotice askAgent (no draft typed), Retry beside it">
        <div className="flex flex-col gap-1.5">
          <div className="flex items-start gap-2.5">
            <ErrorNotice className="flex-1" title="Webhook settings are unavailable" message={WEBHOOKS_LOAD_ERROR} askAgent />
            <Btn className="shrink-0">Retry</Btn>
          </div>
          <p className="m-0 text-[12px] text-muted">The reference below still describes the endpoint.</p>
        </div>
      </Section>
      <Section title="ProjectsPage — ErrorNotice, No hand-off (workspaceDir / refined drafts)">
        <ErrorNotice message={PLAN_ERROR} />
      </Section>
      <Section title="LogsPage — useMutation onError → inline ErrorNotice askAgent, dismissable">
        <ErrorNotice variant="inline" message={LOG_LEVEL_ERROR} askAgent onDismiss={() => {}} />
      </Section>
      <Section title="RemoteArtifactDetailPage — ErrorNotice with title, askAgent (nothing else rendered)">
        <div className="rounded-xl border border-border bg-bg-elevated p-4">
          <ErrorNotice title="Failed to load remote artifact" message={REMOTE_ERROR} askAgent />
          <div className="mt-3"><Btn>Back to library</Btn></div>
        </div>
      </Section>
      <Section title="SessionArchive — ErrorNotice askAgent (list read)">
        <ErrorNotice message={ARCHIVE_ERROR} askAgent />
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
