/**
 * Isolated capture entry for a chat-transcript link whose destination is a
 * local path the gateway reports missing.
 *
 * The transcript bubble holds the REAL `MarkdownRenderer` over a stubbed
 * `/api/file-kinds` probe that answers `missing` for every path, so the link
 * classifies exactly as it does in production. Two populations sit side by side
 * so the screenshot shows the whole rule in one frame:
 *
 *   - a missing FILE link (`/tmp/x.md`, a relative `notes/guide.md` joined to
 *     the chat project, and `~/notes.md`) -- the surface the change touches;
 *   - a root-relative in-app ROUTE link (`/settings/security/rules`) -- which
 *     must keep its accent dress and its navigation.
 *
 * Before the fix every file link renders in the accent link colour with the
 * link underline and opens a new browser tab at a dead dashboard URL. After the
 * fix each file link is muted and non-navigating, matching the inline copy chip
 * for the same path, while the route link is unchanged.
 *
 * Query string: ?theme=dark|light.
 */
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import { ChatProjectDirCtx } from '../src/components/markdown/contexts'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const PROJECT = '/home/user/product-guide'

/** Every path the probe is asked about answers `missing`, the outcome this
 *  scene exists to show. Stubbed at the batch seam the real hook uses
 *  (`POST /api/file-kinds`), with the same `{ kinds: { path: 'missing' } }`
 *  body shape the endpoint returns, so the links classify as they do in
 *  production. */
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url === '/api/file-kinds') {
    let paths: string[] = []
    try { paths = (JSON.parse(String(init?.body ?? '{}')).paths ?? []) as string[] } catch { /* none */ }
    const kinds: Record<string, string> = {}
    for (const p of paths) kinds[p] = 'missing'
    return Promise.resolve(new Response(JSON.stringify({ kinds }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }))
  }
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

const NOTE = [
  'I could not find the config, so here is where it would live.',
  '',
  '- A missing absolute file: [the note](/tmp/x.md)',
  '- A missing file in this project: [the guide](notes/guide.md)',
  '- A missing home file: [my notes](~/notes.md)',
  '',
  'To change it yourself, open [the rules page](/settings/security/rules).',
].join('\n')

/** The transcript's assistant bubble, class list copied from the message row. */
const BUBBLE_CLS = 'msg-content px-4 py-3 text-sm leading-6 rounded-lg bg-bg-elevated text-text ring-1 ring-inset ring-border/60 rounded-bl-[4px] overflow-hidden min-w-0'

function Scene() {
  return (
    <div className="bg-bg text-text flex flex-col min-h-screen">
      <div className="border-b border-border px-4 py-2 text-[13px] text-muted mx-auto w-full" style={{ maxWidth: 760 }}>
        Chat transcript
      </div>
      <div className="px-4 pt-4 mx-auto w-full flex flex-col gap-3" style={{ maxWidth: 760 }}>
        <div className="flex flex-col items-start max-w-[92%]" data-bubble>
          <div className={BUBBLE_CLS}>
            <ChatProjectDirCtx.Provider value={PROJECT}>
              <MarkdownRenderer content={NOTE} onFileOpen={() => {}} />
            </ChatProjectDirCtx.Provider>
          </div>
        </div>
      </div>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
