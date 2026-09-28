/**
 * The Prompts tab's detail pane, in the two shapes its header renders.
 *
 * WHY THIS SCENE EXISTS: the header carries three operations against a
 * two-control budget (`max-two-buttons-per-row`, website/AUTOSDE.yaml), and the
 * resolution is a SPLIT rather than an overflow menu — send sits with the
 * identity it acts on, the edit/destroy pair sits at the far end. That split is
 * a layout claim, and a layout claim is worth a frame: the alternative is
 * arguing about it in prose.
 *
 * The scene mounts the REAL `PromptsTab` against the real stylesheet, theme
 * tokens and live i18n catalog, with only `fetch` stubbed. Nothing here
 * re-implements a button, an icon, a label or a class, so what you click is what
 * ships — Edit really swaps the editor in, and the slot picker really lists the
 * seeded chats.
 *
 *   ?source=user       a writable user prompt (default): send + Edit/Delete
 *   ?source=package    a read-only package SOP: send only, since it has no scope
 *   ?theme=dark|light
 *   ?chrome=off        hide the switcher (for a clean screenshot)
 *
 * Serve it from the ONE vite dev server, no gateway and no session needed. Bind
 * inside 8700-8799, which is the range forwarded to a workstation:
 *   npx vite --host 127.0.0.1 --port 8707 --strictPort
 *   http://127.0.0.1:8707/capture/prompts-header.html
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import PromptsTab from '../src/pages/overview/PromptsTab'
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const packageSop = params.get('source') === 'package'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** A writable user prompt and a read-only package SOP: the two shapes the
 *  header renders differently, since only the first carries a scope. */
const USER_PROMPT = {
  name: 'demo-review-checklist',
  fullName: 'demo-review-checklist',
  package: '',
  source: 'global',
  description: 'Walk a diff before requesting review',
  path: '~/.kiro/prompts/demo-review-checklist.md',
}
const PACKAGE_SOP = {
  name: 'log-investigation',
  fullName: 'aws:log-investigation',
  package: 'aws',
  source: 'package',
  description: 'Trace a failing request from the alarm to the log line',
  path: '/opt/kiro/prompts/aws-log-investigation.sop.md',
}

const BODY = `---
description: Walk a diff before requesting review
---

# Review checklist

Before requesting review on this change:

1. Does each new branch encode a rule, or just the case that prompted it?
2. Is every error response carrying a machine-readable code?
3. Do the tests pin the invariant, not the example?
`

const json = (body: unknown) => Promise.resolve(
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }),
)

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  // The list, then the scoped detail read. `redacted`/`lossy` false, so the
  // editor is reachable — the read-only captions have their own coverage in the
  // frontend tests and are not what this scene is for.
  if (/\/api\/prompts\/[^?]/.test(url)) {
    return json({
      ...(packageSop ? PACKAGE_SOP : USER_PROMPT),
      content: BODY,
      redacted: false,
      lossy: false,
      // The edit base a real detail read hands out; the scene's editor carries
      // it the same way the live one does.
      hash: 'a'.repeat(64),
    })
  }
  if (url.includes('/api/prompts')) return json([USER_PROMPT, PACKAGE_SOP])
  if (url.includes('/api/')) return json({})
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

await initI18n('en')

// Seed the chats the slot picker routes to, so the send control opens onto a
// real list rather than its empty state. `sseSlots` is the same reducer the live
// websocket feeds.
store.dispatch(sseSlots([
  { key: 'dashboard:1', title: 'PR #4634 review', agent: 'meshclaw', running: true },
  { key: 'dashboard:2', title: 'Prompt authoring', agent: 'meshclaw', running: false },
  { key: 'dashboard:3', title: 'Windows shard triage', agent: 'kirocrew', running: false },
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- ChatSlot carries
// ~40 fields the picker never reads; the four it does read are what the scene needs.
] as any))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } })

/**
 * Scene chrome: pick a case without hand-editing the URL.
 *
 * Navigates rather than swapping React state, because both axes are consumed at
 * MODULE level — the fetch stub closes over `packageSop`, and the theme is an
 * attribute on <html> — so a state-only switch would leave the page disagreeing
 * with its own controls. A dev-server reload is a few milliseconds.
 *
 * Rendered OUTSIDE `data-capture-root` so a screenshot runner frames the
 * component and never this switcher. `?chrome=off` hides it entirely.
 */
function Switcher() {
  if (params.get('chrome') === 'off') return null

  const go = (patch: Record<string, string>) => {
    const next = new URLSearchParams(location.search)
    for (const [k, v] of Object.entries(patch)) next.set(k, v)
    location.search = next.toString()
  }
  const group = (
    label: string,
    param: string,
    active: string,
    options: readonly { value: string; label: string }[],
  ) => (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <span style={{ font: '600 10px/1 ui-sans-serif, system-ui', letterSpacing: '.08em',
        textTransform: 'uppercase', color: 'var(--muted)' }}>{label}</span>
      <div style={{ display: 'flex', gap: 4 }} role="radiogroup" aria-label={label}>
        {options.map(o => {
          const on = o.value === active
          return (
            <button
              key={o.value}
              type="button"
              role="radio"
              aria-checked={on}
              onClick={() => go({ [param]: o.value })}
              style={{
                font: '500 12px/1 ui-sans-serif, system-ui',
                padding: '5px 10px',
                borderRadius: 6,
                cursor: on ? 'default' : 'pointer',
                border: `1px solid ${on ? 'var(--accent)' : 'var(--border)'}`,
                background: on ? 'var(--accent-subtle)' : 'transparent',
                color: on ? 'var(--accent)' : 'var(--muted)',
              }}
            >{o.label}</button>
          )
        })}
      </div>
    </div>
  )

  return (
    <div style={{
      display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 18,
      padding: '10px 14px', marginBottom: 16, borderRadius: 8,
      border: '1px dashed var(--border)', background: 'var(--bg-elevated)',
    }}>
      {group('prompt', 'source', packageSop ? 'package' : 'user', [
        { value: 'user', label: 'user (writable)' },
        { value: 'package', label: 'package (read-only)' },
      ])}
      {group('theme', 'theme', theme, [
        { value: 'dark', label: 'dark' },
        { value: 'light', label: 'light' },
      ])}
      <span style={{ font: '400 11px/1.4 ui-sans-serif, system-ui', color: 'var(--muted)' }}>
        scene chrome — not part of the capture
      </span>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <div style={{ width: 1100, padding: 20, background: 'var(--bg)' }}>
    <Provider store={store}>
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <Switcher />
          <div data-capture-root>
            <div style={{
              font: '600 11px/1 ui-sans-serif, system-ui', letterSpacing: '.09em',
              textTransform: 'uppercase', color: 'var(--muted)', marginBottom: 12,
            }}>
              source={packageSop ? 'package' : 'user'} &middot; theme={theme}
            </div>
            <PromptsTab />
          </div>
        </QueryClientProvider>
      </MemoryRouter>
    </Provider>
  </div>,
)
