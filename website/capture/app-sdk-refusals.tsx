/**
 * Isolated capture entry for the App SDK refusals an external app sees when its
 * manifest leaves out a component's reads: PullRequestPanel, ProjectPicker,
 * IssueList and SessionChanges, each in its own frame.
 *
 * The first four frames declare nothing, so they send no request. The "diff"
 * frame declares only SessionChanges' list read: its one list request is
 * answered here, and the shoot script picks the file so the diff pane shows its
 * own refusal. The components, the app's scoped client, the locale catalogue
 * and the stylesheet are the real ones.
 *
 * Query: ?theme=dark|light&lang=<locale> (default dark, en)
 */
import { createRoot } from 'react-dom/client'
import type { ReactNode } from 'react'

import { initI18n, i18next } from '../src/i18n/all'
import { AppApiProvider, IssueList, ProjectPicker, PullRequestPanel, SessionChanges } from '../src/app-sdk/index'
import { SESSION_CHANGES_PATHS } from '../src/app-sdk/SessionChanges'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const lang = params.get('lang') || 'en'
initI18n(lang)
// initI18n resolves the stored/boot language; force the requested one before
// the first render, so a shot never catches the English fallback.
await i18next.changeLanguage(lang)
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

const DIR = '/home/u/proj'
const listing = {
  files: [{ path: `${DIR}/src/app.ts`, ts: '2026-11-21T12:00:00Z', exists: true }],
  truncated: false,
}
const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href)
  if (url.pathname !== SESSION_CHANGES_PATHS.sessionFiles) return realFetch(input, init)
  return new Response(JSON.stringify(listing), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

const none = () => {}

function Frame({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <section data-testid={`frame-${id}`} className="rounded border border-border p-3">
      <div className="mb-2 text-xs text-text-muted">{label}</div>
      {children}
    </section>
  )
}

createRoot(document.getElementById('root')!).render(
  <div className="min-h-screen bg-bg p-4 text-text">
    <div data-testid="frames" className="mx-auto flex max-w-[720px] flex-col gap-3">
      <AppApiProvider appName="refusals-demo" allowedApiPaths={[]} navigateFn={none}>
        <Frame id="pr" label="PullRequestPanel">
          <PullRequestPanel sources={[]} selectedUrl="" onSelect={none} />
        </Frame>
        <Frame id="picker" label="ProjectPicker">
          <ProjectPicker open onOpenChange={none} onSelect={none} />
        </Frame>
        <Frame id="issues" label="IssueList">
          <IssueList query={{ provider: 'github', scope: 'acme/widgets' }} />
        </Frame>
        <Frame id="changes" label="SessionChanges (list not declared)">
          <SessionChanges slotKey="dashboard:chat-1" projectDir={DIR} />
        </Frame>
      </AppApiProvider>
      <AppApiProvider appName="refusals-demo-diff" allowedApiPaths={[SESSION_CHANGES_PATHS.sessionFiles]} navigateFn={none}>
        <Frame id="diff" label="SessionChanges (list declared, diff not declared)">
          <div className="h-[320px]"><SessionChanges slotKey="dashboard:chat-2" projectDir={DIR} /></div>
        </Frame>
      </AppApiProvider>
    </div>
  </div>,
)
