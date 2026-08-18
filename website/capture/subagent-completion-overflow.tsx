/**
 * Visual evidence for "a long wave digest stays inside the completion card".
 *
 * WHY ISOLATED: reproducing the live defect means fanning out 7+ sub-agents
 * and catching the partial batch event while a tool group still streams below
 * it — not reproducible on demand, and a half-seeded ChatPage draws its error
 * boundary instead of the rows.
 *
 * WHAT IS FAITHFUL is the CONTAINMENT, since that is the whole claim: the card
 * is the real component, expanded on a real long digest (one block per agent),
 * inside the literal host column wrapper (`px-4 mx-auto w-full py-1` under
 * `--mc-content-width`, ChatPage.tsx), with a sibling row below it standing in
 * for the tool calls the unbounded card used to paint over.
 *
 * The `before` scene neutralizes exactly the two containment classes the fix
 * adds (max-height/overflow-y on the body) via an injected stylesheet — the
 * card then renders precisely what the pre-fix code did: an unbounded body
 * that swallows the space the sibling row needs. `after` is the current code.
 *
 *   ?scene=before|after &theme=dark|light
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6815 --strictPort
 *   node scripts/capture-subagent-completion-overflow.mjs http://127.0.0.1:6815 \
 *     ../temp-screenshots/subagent-completion-overflow
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import SubagentCompletionCard from '../src/pages/chat/SubagentCompletionCard'
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
// the two classes the fix adds is not a mock of the bug — an unbounded body is
// literally what the old markup produced.
if (scene === 'before') {
  const style = document.createElement('style')
  style.textContent =
    '[data-testid="subagent-completion-body"]' +
    '{ max-height: none !important; overflow-y: visible !important; }'
  document.head.appendChild(style)
}

const msg = (content: string, meta?: Record<string, unknown>): ChatMessage =>
  ({ role: 'subagent', content, cls: '', ts: '2026-08-18T00:00:00.000Z', meta })

// The issue's shape: a PARTIAL batch event mid-wave, its digest one block per
// delivered agent with an error excerpt or a result path under each — the tall
// machine-composed payload that grew the card past its row.
const TASKS = [
  'Audit HooksPage for the sticky Actions seam and report the class deltas',
  'Audit SchedulePage column contract tests against the shared loadSource helper',
  'Validate the i18n catalogs for the 12 locales against the parity checker',
  'Re-run the electron bundle-location suite and summarize the 24 assertions',
  'Sweep transcriptRenderers for cards that re-apply the host column geometry',
  'Check QueueStack delivery filtering against the injected-messages spec',
  'Profile the virtualizer measurement path on a 5k-message transcript',
  'Verify MarkdownRenderer softBreaks output against the digest line structure',
  'Cross-check the disclosure persistence keys across ChatPage and ChatPane',
  'Run the backend pytest shard for the subagent delivery batching module',
  'Compare the embed SDK card rendering against the dashboard host wrapper',
  'Collect bundle-size deltas for the chat page after the containment change',
]
const DIGEST_ROWS = TASKS.map((t, i) =>
  i === 1
    ? `— \`b8185d${i}5\` failed ❌ · ${t}\n  Error: catalog parity check failed on 3 of 12 locales, see the per-locale diff at /tmp/kc-audit/${i}/parity.diff for the full breakdown`
    : `— \`53e3e5e${i}\` ✅ ${t}\n  → /home/u/.kiro/crew/subagents/53e3e5e${i}/result.txt`,
).join('\n')

const BATCH = [
  '[Subagent batch completion event]',
  'Batch results 1/2 — 12 of 19 delivered, 7 still running.',
  'Process these results now, but do NOT spawn new sub-agents yet — more result batches from this run are still arriving, and spawning now will interleave with them.',
  'Failures are listed first. Full outputs are on disk — read the result paths on demand; do NOT re-run completed agents.',
  '',
  DIGEST_ROWS,
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

/** Stand-in for the tool rows streaming below the event in the issue. */
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
            <Row label="partial batch event (12 delivered, digest expanded)">
              {/* A mid-wave chunk parses with failed=0 (only the final chunk
                  carries tallies), so the card mounts FOLDED — the capture
                  runner clicks the disclosure open, the way a reader would. */}
              <SubagentCompletionCard message={msg(BATCH)} disclosureKey="cap-sc" />
            </Row>
            <Row label="tool rows still streaming below (the overlap victims)">
              <div data-sibling>
                <SiblingToolRow label="fs_read · website/src/pages/chat/transcriptRenderers.tsx" />
                <SiblingToolRow label="grep · SubagentCompletionCard --include=*.tsx" />
                <SiblingToolRow label="execute_bash · npx vitest run src/test/SubagentCompletionCard.test.tsx" />
              </div>
            </Row>
          </div>
        </div>
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
