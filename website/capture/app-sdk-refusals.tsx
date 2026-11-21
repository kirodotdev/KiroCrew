/**
 * Isolated capture entry for the App SDK refusals an external app sees when its
 * manifest declares none of a component's reads: PullRequestPanel,
 * ProjectPicker, IssueList and SessionChanges, each in its own frame.
 *
 * Nothing is declared, so none of them sends a request; the components, the
 * app's scoped client, the locale catalogue and the stylesheet are the real ones.
 *
 * Query: ?theme=dark|light&lang=<locale> (default dark, en)
 */
import { createRoot } from 'react-dom/client'
import type { ReactNode } from 'react'

import { initI18n, i18next } from '../src/i18n/all'
import { AppApiProvider, IssueList, ProjectPicker, PullRequestPanel, SessionChanges } from '../src/app-sdk/index'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const lang = params.get('lang') || 'en'
initI18n(lang)
// initI18n resolves the stored/boot language; force the requested one before
// the first render, so a shot never catches the English fallback.
await i18next.changeLanguage(lang)
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

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
    <AppApiProvider appName="refusals-demo" allowedApiPaths={[]} navigateFn={none}>
      <div data-testid="frames" className="mx-auto flex max-w-[720px] flex-col gap-3">
        <Frame id="pr" label="PullRequestPanel">
          <PullRequestPanel sources={[]} selectedUrl="" onSelect={none} />
        </Frame>
        <Frame id="picker" label="ProjectPicker">
          <ProjectPicker open onOpenChange={none} onSelect={none} />
        </Frame>
        <Frame id="issues" label="IssueList">
          <IssueList query={{ provider: 'github', scope: 'acme/widgets' }} />
        </Frame>
        <Frame id="changes" label="SessionChanges">
          <SessionChanges slotKey="dashboard:chat-1" projectDir="/home/u/proj" />
        </Frame>
      </div>
    </AppApiProvider>
  </div>,
)
