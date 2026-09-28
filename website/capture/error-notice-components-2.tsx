/**
 * Isolated capture entry for the components-2 batch of the error-state sweep
 * (`src/components/*`: file-path menu, folder config modal, follow-up card,
 * git panel, issue panel, sign-in / prerequisite gates, MCP browser modal,
 * mobile connect modal, pending question card).
 *
 * WHY ISOLATED: every one of these surfaces only shows its failed state once a
 * gateway request has actually failed (react-query, `api.*`, redux), and
 * several live inside modals, context menus and gates that need a session to
 * reach. The surfaces are shown here as the exact `ErrorNotice` calls this
 * branch makes, with the same strings the components pass.
 *
 * Two rows of two columns per frame (row 2 carries the surfaces that only
 * appear once a menu, modal or gate is open):
 *   BEFORE — the hand-written surfaces reconstructed verbatim from origin/main
 *            (bare `text-danger` / literal `text-red-400` / `text-amber-400`
 *            spans, a hand-built `role="alert"` box, a red `<pre>`, a blocking
 *            `alert()`, and silent failures with nothing on screen at all).
 *   AFTER  — the shared `ErrorNotice`, with its agent hand-off where the
 *            surface holds no draft and the `No hand-off` decision where it does.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import type { ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { AlertTriangle, TriangleAlert } from 'lucide-react'

// `../src/i18n/all` registers every language catalog (plain `../src/i18n` is
// English-only), as the shared entry contract requires of every capture.
import { initI18n } from '../src/i18n/all'
import ErrorNotice from '../src/components/ErrorNotice'
import { Btn } from '../src/components/ui'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const FOLDER_SAVE_ERROR = 'HTTP 400: project_dir must be an existing directory'
const WORKTREE_ERROR = 'Branch already exists: feat/upload-rate-limit'
const GIT_STATUS_ERROR = 'HTTP 500: git is not installed on the gateway host'
const ISSUE_ERROR = 'glab: 404 project not found'
const REPAIR_ERROR = 'FileNotFoundError: no shipped defaults.json for kirocrew.json'
const MCP_INSTALL_ERROR = 'HTTP 503: provider unavailable'
const MOBILE_ERROR = 'Could not create a link. Try again.'
const REVEAL_BLOCKED = 'This path is protected and can’t be opened.'
const ANSWER_FAILED = 'Couldn’t send your answer. The agent is still waiting, so try again.'

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="m-0 text-[11px] font-semibold uppercase tracking-wider text-muted">{title}</h3>
      {children}
    </section>
  )
}

function Column({ label, col, children }: { label: string; col: 'before' | 'after'; children: ReactNode }) {
  return (
    <div data-col={col} className="flex-1 min-w-0 flex flex-col gap-6 rounded-xl border border-border bg-card p-4">
      <div className="text-[12px] font-semibold text-text">{label}</div>
      {children}
    </div>
  )
}

/** The origin/main shapes, reconstructed so the diff is visible side by side. */
function Before() {
  return (
    <Column label="Before (origin/main)" col="before">
      <Section title="FolderConfigModal — hand-built role=alert box">
        <div role="alert" className="flex items-start gap-2 text-[11.5px] text-text bg-danger-subtle border border-danger rounded-lg px-3 py-2">
          <TriangleAlert size={13} className="shrink-0 mt-[1px] text-danger" />
          <span className="min-w-0 break-words">{FOLDER_SAVE_ERROR}</span>
        </div>
      </Section>
      <Section title="FollowUpCard — bare text-danger div">
        <div role="alert" className="text-[12px] text-danger mt-2">{WORKTREE_ERROR}</div>
      </Section>
      <Section title="GitPanel — silent (empty panel on a failed read)">
        <div className="text-[12px] text-muted italic">(nothing on screen; the panel looks like a clean repo with no history)</div>
      </Section>
      <Section title="IssuePanel — hand-rolled card, message in a muted <div>">
        <div role="alert" className="max-w-md flex flex-col items-center">
          <AlertTriangle className="lucide-inline mb-2 text-danger" aria-hidden="true" />
          <div className="text-[13px] font-medium text-text">Could not load this issue</div>
          <div className="mt-2 w-full max-h-64 overflow-y-auto rounded-md bg-bg-hover/50 border border-border px-3 py-2 text-left text-[12px] text-muted whitespace-pre-wrap break-words font-mono leading-relaxed">{ISSUE_ERROR}</div>
          <Btn className="mt-3">Retry</Btn>
        </div>
      </Section>
      <Section title="KiroPrerequisiteGate — red <pre> under an uppercase label">
        <div className="w-full max-w-lg text-left" role="alert">
          <p className="m-0 text-[11px] font-semibold uppercase tracking-[0.14em] text-danger">The repair attempt failed</p>
          <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-danger/10 p-3 text-xs text-danger">{REPAIR_ERROR}</pre>
        </div>
      </Section>
      <Section title="McpBrowserModal — literal text-red-400 / text-amber-400">
        <p className="m-0 text-xs text-red-400">{MCP_INSTALL_ERROR}</p>
        <span className="flex items-center gap-1 text-xs text-amber-400" role="status">
          <AlertTriangle size={13} aria-hidden="true" /> Name in use
        </span>
      </Section>
      <Section title="MobileConnectModal — bare text-danger <p role=alert>">
        <p className="m-0 text-[11.5px] text-danger" role="alert">{MOBILE_ERROR}</p>
      </Section>
      <Section title="FilePathMenu / OfficeCard — blocking alert()">
        <div className="text-[12px] text-muted italic">(a native alert() dialog; nothing stays on screen after OK)</div>
      </Section>
      <Section title="PendingQuestionCard — silent (card stays, no message)">
        <div className="text-[12px] text-muted italic">(nothing on screen; the same card, buttons re-enabled)</div>
      </Section>
    </Column>
  )
}

function After() {
  return (
    <Column label="After (this branch)" col="after">
      <Section title="FolderConfigModal — ErrorNotice, No hand-off (folder form draft)">
        <ErrorNotice message={FOLDER_SAVE_ERROR} testId="after-folder" />
      </Section>
      <Section title="FollowUpCard — inline ErrorNotice askAgent (slot draft is persisted)">
        <ErrorNotice variant="inline" className="mt-2 whitespace-normal" message={WORKTREE_ERROR} askAgent />
      </Section>
      <Section title="GitPanel — read failure now renders, askAgent">
        <ErrorNotice title="Couldn’t read the repository status." message={GIT_STATUS_ERROR} askAgent />
      </Section>
      <Section title="IssuePanel — ErrorNotice with title, askAgent, Retry beneath">
        <div className="max-w-md w-full flex flex-col items-center">
          <ErrorNotice className="w-full leading-relaxed" messageClassName="font-mono" title="Could not load this issue" message={ISSUE_ERROR} askAgent />
          <Btn className="mt-3">Retry</Btn>
        </div>
      </Section>
      <Section title="KiroPrerequisiteGate — ErrorNotice title + verbatim body, No hand-off (gate hides the chat), plain remedy beneath">
        <ErrorNotice className="w-full max-w-lg text-left text-xs" messageClassName="font-mono" title="The repair attempt failed" message={REPAIR_ERROR} />
        <p className="m-0 max-w-lg text-left text-[13px] leading-relaxed text-muted">Fix the cause named above (often a permissions or install problem), then press Check again to try again. If it keeps failing, reinstall Kiro CLI.</p>
        <Btn className="self-start">Check again</Btn>
      </Section>
      <Section title="McpBrowserModal — inline ErrorNotice askAgent (design tokens, not literal reds)">
        <ErrorNotice variant="inline" className="mt-1 whitespace-normal" message={MCP_INSTALL_ERROR} askAgent />
        <ErrorNotice variant="inline" className="text-xs" message="Name in use" askAgent />
      </Section>
      <Section title="MobileConnectModal — inline ErrorNotice askAgent">
        <ErrorNotice variant="inline" className="text-[11.5px]" message={MOBILE_ERROR} askAgent />
      </Section>
      <Section title="FilePathMenu / OfficeCard — in-place notice, askAgent, dismissable">
        <ErrorNotice variant="inline" className="whitespace-normal" message={REVEAL_BLOCKED} askAgent onDismiss={() => {}} />
      </Section>
      <Section title="PendingQuestionCard — ErrorNotice under the card, No hand-off (answers unsaved)">
        <ErrorNotice className="mt-2" message={ANSWER_FAILED} onDismiss={() => {}} />
      </Section>
    </Column>
  )
}

const SEARCH_FAILED = 'Couldn’t search files. Try again.'
const JSON_ERROR = 'Unexpected token \'}\' at position 41'
const INSTANCES_ERROR = 'HTTP 502: gateway unavailable'
const TUNNEL_ERROR = 'ssh: connect to host devbox port 22: Connection refused'
const CANCEL_UNSETTLED = 'Couldn’t cancel the sign-in. It is still active — press the same option again to retry.'
const CHANNEL_ERROR = 'Couldn’t connect to Discord: this conversation is already held by another session.'
const TRACE_ERROR = 'HTTP 404: run r-8ad2 has no trace'
const ACTION_ERROR = 'File is too large to add'
const KNOWLEDGE_ERROR = 'Couldn’t check the knowledge library.'
const MERMAID_ERROR = 'Couldn’t render this diagram. Showing the source instead.'
const REGISTRY_ERROR = 'Couldn’t search the registry. Try again.'
const SPEC_ERROR = 'HTTP 400: command must be a non-empty string'
const RELAY_ERROR = 'Could not complete the connection. Paste the address again.'
const ONBOARDING_ERROR = 'Couldn’t save your answers. Press Next to retry or Skip to continue.'

function Silent({ children }: { children: ReactNode }) {
  return <div className="text-[12px] text-muted italic">{children}</div>
}

/** Row 2 — the surfaces the first sheet did not show. */
function Before2() {
  return (
    <Column label="Before (origin/main) — remaining surfaces" col="before">
      <Section title="FilePickerMenu — failed search shows the ordinary empty state">
        <div role="status" className="px-3 py-3 text-[12px] text-muted">No matching files — Enter sends the message</div>
      </Section>
      <Section title="FileRenderers JsonViewer — bold red line">
        <div className="text-danger font-semibold font-mono mb-2">Invalid JSON {JSON_ERROR}</div>
      </Section>
      <Section title="InstanceTabBar — silent (bar hidden on a failed list read)">
        <Silent>(nothing on screen; a failed read and no remote crews look identical)</Silent>
      </Section>
      <Section title="InstancesViewport — muted box + separate Ask-agent button">
        <div className="w-full rounded-md border border-border bg-bg-hover px-3 py-2 text-left text-xs text-muted whitespace-pre-wrap">{TUNNEL_ERROR}</div>
      </Section>
      <Section title="KasLoginGate — silent (cancel did not settle, buttons re-enabled)">
        <Silent>(nothing on screen; the waiting screen simply stays)</Silent>
      </Section>
      <Section title="LinkedSurfacesSection — bell-feed toast only">
        <Silent>(a notification in the bell feed; nothing in the menu)</Silent>
      </Section>
      <Section title="LogEntry — silent (empty <pre>)">
        <pre className="p-2.5 bg-bg-elevated border border-border rounded-md text-[12px] font-mono min-h-[28px]">{''}</pre>
      </Section>
      <Section title="MarkdownPanel — alert() dialog; background reads silent">
        <Silent>(a native alert() for the action; “not added” for a failed knowledge read)</Silent>
      </Section>
      <Section title="MarkdownRenderer — Mermaid source painted red">
        <pre className="text-danger text-[13px] m-0">{'graph TD;\n  A-->B'}</pre>
      </Section>
      <Section title="McpBrowserModal — failed search shows “No servers found”">
        <div className="text-[12px] text-muted">No servers found for “sqlite”</div>
      </Section>
      <Section title="McpCustomServerModal — literal text-amber-400 role=alert span">
        <span className="flex items-center gap-1 text-xs text-amber-400" role="alert"><AlertTriangle size={13} aria-hidden="true" /> {SPEC_ERROR}</span>
      </Section>
      <Section title="OAuthRelayAffordance — bare text-danger <p role=alert>">
        <p className="m-0 text-[12px] leading-4 text-danger" role="alert">{RELAY_ERROR}</p>
      </Section>
      <Section title="OnboardingFlow — inline style color: var(--danger)">
        <p role="alert" className="m-0 text-[12.5px]" style={{ color: 'var(--danger)' }}>{ONBOARDING_ERROR}</p>
      </Section>
    </Column>
  )
}

function After2() {
  return (
    <Column label="After (this branch) — remaining surfaces" col="after">
      <Section title="FilePickerMenu — inline ErrorNotice, No hand-off (composer draft)">
        <ErrorNotice variant="inline" className="whitespace-normal" message={SEARCH_FAILED} />
      </Section>
      <Section title="FileRenderers JsonViewer — ErrorNotice title + parser message, askAgent">
        <ErrorNotice messageClassName="font-mono" title="Invalid JSON" message={JSON_ERROR} askAgent />
      </Section>
      <Section title="InstanceTabBar — inline ErrorNotice askAgent (bar stays up to say so)">
        <ErrorNotice variant="inline" className="whitespace-normal" message={INSTANCES_ERROR} askAgent />
      </Section>
      <Section title="InstancesViewport — ErrorNotice report + askAgent + onHandoff (→ Local)">
        <ErrorNotice className="w-full text-left text-xs" message={TUNNEL_ERROR} askAgent />
      </Section>
      <Section title="KasLoginGate — ErrorNotice, No hand-off (sign-in gate hides the chat)">
        <ErrorNotice className="max-w-md" message={CANCEL_UNSETTLED} />
      </Section>
      <Section title="LinkedSurfacesSection — in place under the row, askAgent, dismissable">
        <ErrorNotice variant="inline" className="whitespace-normal text-[11px]" message={CHANNEL_ERROR} askAgent onDismiss={() => {}} />
      </Section>
      <Section title="LogEntry — ErrorNotice title + reason, askAgent">
        <ErrorNotice title="Couldn’t load this run’s trace." message={TRACE_ERROR} askAgent />
      </Section>
      <Section title="MarkdownPanel — one panel-level notice (askAgent while the buffer is clean)">
        <div className="flex flex-col gap-1.5">
          <ErrorNotice message={ACTION_ERROR} askAgent onDismiss={() => {}} />
          <ErrorNotice message={KNOWLEDGE_ERROR} askAgent />
        </div>
      </Section>
      <Section title="MarkdownRenderer — inline ErrorNotice above the (now muted) source, No hand-off">
        <ErrorNotice variant="inline" className="mb-2" message={MERMAID_ERROR} />
        <pre className="text-muted text-[13px] m-0">{'graph TD;\n  A-->B'}</pre>
      </Section>
      <Section title="McpBrowserModal — search failure is an ErrorNotice, askAgent">
        <ErrorNotice message={REGISTRY_ERROR} askAgent />
      </Section>
      <Section title="McpCustomServerModal — inline ErrorNotice, No hand-off (spec JSON draft)">
        <ErrorNotice variant="inline" className="text-xs" message={SPEC_ERROR} />
      </Section>
      <Section title="OAuthRelayAffordance — inline ErrorNotice, No hand-off (pasted address)">
        <ErrorNotice variant="inline" className="text-[12px] whitespace-normal" message={RELAY_ERROR} />
      </Section>
      <Section title="OnboardingFlow — ErrorNotice, No hand-off (wizard answers)">
        <ErrorNotice className="text-[12.5px]" message={ONBOARDING_ERROR} />
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
        <div className="flex flex-col gap-6" style={{ maxWidth: 1080 }}>
          <div className="flex gap-4 items-start">
            <Before />
            <After />
          </div>
          <div className="flex gap-4 items-start">
            <Before2 />
            <After2 />
          </div>
        </div>
      </div>
    </MemoryRouter>,
  )
}

void main()
