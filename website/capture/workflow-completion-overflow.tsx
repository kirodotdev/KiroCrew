/**
 * Visual evidence for "a long workflow result stays inside the completion card".
 *
 * WHY ISOLATED: reproducing the live defect means running a background workflow
 * to completion so the backend injects the completion message into a live chat —
 * not reproducible on demand, and a half-seeded ChatPage draws its error
 * boundary instead of the rows.
 *
 * WHAT IS FAITHFUL is the CONTAINMENT, since that is the whole claim: the card
 * is the real component, expanded on a real long result (the machine-composed
 * injected payload shape from dashboard/workflow_inject.py), inside the literal
 * host column wrapper (`px-4 mx-auto w-full py-1` under `--mc-content-width`,
 * ChatPage.tsx), with a sibling row below it standing in for the transcript
 * rows the unbounded card used to paint over.
 *
 * The `before` scene neutralizes exactly the two containment classes the fix
 * adds (max-height/overflow-y on the body) via an injected stylesheet — the
 * card then renders precisely what the pre-fix code did: an unbounded body
 * that swallows the space the sibling row needs. `after` is the current code.
 *
 *   ?scene=before|after &theme=dark|light
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6816 --strictPort
 *   node scripts/capture-workflow-completion-overflow.mjs http://127.0.0.1:6816 \
 *     ../temp-screenshots/workflow-completion-overflow
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import WorkflowCompletionCard from '../src/pages/chat/WorkflowCompletionCard'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'before' ? 'before' : 'after'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// The expanded card mounts MarkdownRenderer, which probes path-like inline
// code and unfurls links. Neither endpoint exists here, and a pending probe
// leaves a chip mid-load, so answer both deterministically.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/file-read')) {
    return Promise.resolve(new Response(null, { status: 200, headers: { 'X-Path-Kind': 'file' } }))
  }
  if (url.startsWith('/api/link-meta')) return Promise.resolve(Response.json({}))
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

// `before` = the pre-fix body: no height bound, no inner scroll. Neutralizing
// the classes the fix adds is not a mock of the bug — an unbounded body is
// literally what the old markup produced. overflow-x must be reset too: with
// it left hidden, the y-axis `visible` would compute to `auto` and the before
// body would be a scroll container the pre-fix markup never was.
if (scene === 'before') {
  const style = document.createElement('style')
  style.textContent =
    '[data-testid="workflow-completion-body"]' +
    '{ max-height: none !important; overflow-y: visible !important; overflow-x: visible !important; }'
  document.head.appendChild(style)
}

const msg = (content: string): ChatMessage =>
  ({ role: 'assistant', content, cls: '', ts: '2026-08-18T00:00:00.000Z' })

// The issue's shape: an injected completion whose Result block is the
// workflow's full machine-composed output — one section per phase with per-agent
// outcomes under each — the tall payload that grew the card past its row.
const PHASES = [
  'inventory: enumerate the transcript renderers registered for injected events',
  'audit: check each card for a bounded expanded body and an inner scroll',
  'cross-check: compare the class lists against the containment contract',
  'report: emit one block per finding with the owning file and line range',
]
const RESULT_ROWS = PHASES.map((p, i) =>
  [
    `### Phase ${i + 1} — ${p}`,
    ...Array.from({ length: 6 }, (_, j) =>
      j === 4 && i === 2
        ? `- agent ${j + 1}: FAILED — class contract mismatch, see \`/tmp/kc-audit/${i}/${j}/diff.txt\` for the per-element breakdown of the containment classes that did not survive`
        : `- agent ${j + 1}: ok — result at \`/tmp/kc-audit/${i}/${j}/result.txt\``,
    ),
  ].join('\n'),
).join('\n\n')

const COMPLETION = [
  '[Workflow completion event]',
  'Workflow `renderer-containment-audit` (wf_capture01) → **finished**',
  '',
  'Result:',
  '',
  RESULT_ROWS,
  '',
  "Use workflow_result('wf_capture01') for the full event stream.",
].join('\n')

const COLUMN = { maxWidth: 'var(--mc-content-width, 900px)' } as const

/** One transcript row in the literal host wrapper (ChatPage.tsx). */
function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <div className="px-4 mx-auto w-full" style={COLUMN}>
        <div className="text-[10px] uppercase tracking-wider text-accent/70 pt-2 pb-0.5 font-mono">{label}</div>
      </div>
      <div data-row={label} className="px-4 mx-auto w-full py-1" style={COLUMN}>
        {children}
      </div>
    </>
  )
}

/** Stand-in for the transcript rows streaming below the event in the issue. */
function SiblingToolRow({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 text-[13px] text-muted font-mono px-1 py-0.5">
      <span className="inline-block w-3 h-3 rounded-full border-2 border-accent/50 border-t-transparent animate-spin" aria-hidden />
      <span>{label}</span>
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <div
          data-capture-root
          data-scene={scene}
          className="bg-bg text-text relative"
          style={{ width: 900, ['--mc-content-width' as string]: '800px' }}
        >
          <div className="py-4">
            <Row label="workflow completion event (result expanded)">
              {/* The card mounts FOLDED (result behind "Show result") — the
                  capture runner clicks the disclosure open, the way a reader
                  would. */}
              <WorkflowCompletionCard message={msg(COMPLETION)} disclosureKey="cap-wf" />
            </Row>
            <Row label="tool rows still streaming below (the overlap victims)">
              <div data-sibling>
                <SiblingToolRow label="fs_read · website/src/pages/chat/WorkflowCompletionCard.tsx" />
                <SiblingToolRow label="grep · workflow-completion-body --include=*.tsx" />
                <SiblingToolRow label="execute_bash · npx vitest run src/test/WorkflowCompletionCard.test.tsx" />
              </div>
            </Row>
          </div>
        </div>
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
