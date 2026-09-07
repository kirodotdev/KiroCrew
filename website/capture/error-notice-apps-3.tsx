/**
 * Isolated capture entry for the apps-3 batch of the error-state sweep
 * (ops-mission-control, papyrus, personal-shopper, pptx-maker, spec-builder,
 * workflows, mochi, TrustAppModal, Library page).
 *
 * WHY ISOLATED: these failure states only appear once a request against a
 * provider or the gateway fails; booting the full SPA for that needs the app
 * shell, a gateway and per-app backends. `WorkflowRunTree` is prop-driven, so
 * its failed state renders directly with the value the app would hand it; the
 * hook-driven views (ops-mission-control board, papyrus workspace, shopper
 * tabs, pptx library) are shown as the exact `ErrorNotice` call this branch
 * makes at each site.
 *
 * Two columns per frame:
 *   BEFORE — the hand-written surfaces reconstructed verbatim from origin/main
 *            (bare `text-danger` <p>s and spans, a hand-built bg-danger/10 box,
 *            a red-bordered failure panel, a toast-only failure with no in-page
 *            surface).
 *   AFTER  — the shared `ErrorNotice`, with its agent hand-off where the
 *            surface holds no draft and the `No hand-off` decision where it does.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import type { ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { AlertTriangle, X, XCircle } from 'lucide-react'

// `../src/i18n/all` registers every language catalog (plain `../src/i18n` is
// English-only), as the shared entry contract requires of every capture.
import { initI18n } from '../src/i18n/all'
import ErrorNotice from '../src/components/ErrorNotice'
import WorkflowRunTree from '../src/apps/workflows/WorkflowRunTree'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const DISPATCH_ERROR = 'HTTP 502: provider poll timed out after 30s'
const CLAIM_ERROR = 'HTTP 409: INV-42 is already claimed by another instance'
const COMPILE_ERROR = 'latexmk exited with code 12: main.tex not found'
const SAVE_ERROR = 'HTTP 500: could not write sites.json'
const RUN_ERROR = 'Agent "researcher" exceeded its token budget'

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
      <Section title="OpsMissionControlPage — dispatch failure, bare text-danger p">
        <p className="text-[13px] text-danger mb-4">{DISPATCH_ERROR}</p>
      </Section>
      <Section title="SignalsPanel — claim failure, bare text-danger p">
        <p className="text-[12px] text-danger mt-2">{CLAIM_ERROR}</p>
      </Section>
      <Section title="PapyrusPage — hand-built bg-danger/10 box with its own dismiss">
        <div className="bg-danger/10 border border-danger/20 rounded-lg p-2.5 flex items-start gap-3" role="alert">
          <AlertTriangle className="lucide-inline text-danger shrink-0 mt-0.5" />
          <div className="flex-1 text-[13px] text-text break-words">{COMPILE_ERROR}</div>
          <button type="button" className="p-1 rounded text-muted bg-transparent border-none">
            <X className="lucide-inline" />
          </button>
        </div>
      </Section>
      <Section title="SitesTab — hand-built danger-subtle div">
        <div role="alert" className="text-xs px-3 py-2 rounded-lg bg-[var(--danger-subtle)] text-[var(--danger)] border border-[var(--danger)]">
          Save failed: {SAVE_ERROR}
        </div>
      </Section>
      <Section title="WorkflowRunTree — red-bordered failure panel">
        <div className="text-[12px] rounded p-2 border border-red-500/30 text-red-500">
          <div className="font-medium mb-1 flex items-center gap-1.5">
            <XCircle size={12} />
            Failed: {RUN_ERROR}
          </div>
        </div>
      </Section>
      <Section title="PreferencesTab — add / delete rejections had no surface">
        <div className="text-[12px] text-muted italic">(nothing rendered; the input just did not clear)</div>
      </Section>
    </Column>
  )
}

function After() {
  return (
    <Column label="After (this branch)">
      <Section title="OpsMissionControlPage — ErrorNotice askAgent (dispatch inputs are persisted)">
        <ErrorNotice message={DISPATCH_ERROR} askAgent />
      </Section>
      <Section title="SignalsPanel — ErrorNotice askAgent (status panel, no draft)">
        <ErrorNotice message={CLAIM_ERROR} askAgent />
      </Section>
      <Section title="PapyrusPage — ErrorNotice, dismissable, No hand-off (open editor buffer)">
        <ErrorNotice message={COMPILE_ERROR} onDismiss={() => {}} />
      </Section>
      <Section title="SitesTab — ErrorNotice, hand-off gated on the site form being empty">
        <ErrorNotice message={`Save failed: ${SAVE_ERROR}`} askAgent />
      </Section>
      <Section title="WorkflowRunTree — real component, failed run through ErrorNotice askAgent">
        <WorkflowRunTree events={[]} status="failed" error={RUN_ERROR} />
      </Section>
      <Section title="PreferencesTab — ErrorNotice, No hand-off (new-preference input)">
        <ErrorNotice message="HTTP 500: preference store is read-only" />
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
