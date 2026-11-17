/**
 * Isolated capture entry for the KAS folder-agent hooks notice (PR evidence).
 *
 * The real `ProjectHooksNotice` in each state the gateway sends: `off` (Turn on),
 * `on` (Turn off) and `kiro_cli` (Turn off). The copy is the gateway's own text
 * (`turn_spec_hooks._project_hooks_notice_text`). A click succeeds through the stub
 * below and shows the saved state; `?hold=1` holds it at saving, `?fail=1` shows the
 * failure state with the capture server's own reason. `?theme=light` for light.
 */
import { createRoot } from 'react-dom/client'

// Success stub for the consent route, so a click reaches the saving state (held
// while `?hold=1`) and then the saved state, and the card's read-back answers from
// the same per-folder record. `?fail=1` keeps the real requests, which the capture
// server has no route for, to show the failure state.
const params = new URLSearchParams(location.search)
const record: Record<string, boolean> = { '/work/off': false, '/work/on': true, '/work/kiro_cli': true }
if (params.get('fail') !== '1') {
  const realFetch = window.fetch.bind(window)
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if (!url.includes('/api/project-hooks/consent')) return realFetch(input, init)
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
    if ((init?.method ?? 'GET').toUpperCase() === 'GET') {
      const folder = new URL(url, location.href).searchParams.get('folder') ?? ''
      return json({ state: record[folder] ? 'on' : 'off', enabled: Boolean(record[folder]) })
    }
    if (params.get('hold') === '1') await new Promise(() => {})
    const { folder, enabled } = JSON.parse(String(init?.body ?? '{}'))
    record[folder] = Boolean(enabled)
    return json({ ok: true, enabled: Boolean(enabled) })
  }
}
import { initI18n } from '../src/i18n/all'
import ProjectHooksNotice from '../src/pages/chat/ProjectHooksNotice'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const theme = new URLSearchParams(location.search).get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme)

const SOURCE =
  "This repo's agent file /work/repo/.kiro/agents/repo-guard.json gives agent repo-guard checks that run as commands on this computer before and after its tool calls and around your messages."

const TEXT: Record<string, string> = {
  off: `ℹ Project hooks are off for this folder. ${SOURCE} Open Show commands to read them first.`,
  on: `ℹ Project hooks are running because you turned them on for this folder. ${SOURCE}`,
  kiro_cli: `ℹ Project hooks are running because you trusted this folder in kiro-cli. ${SOURCE} Turn off stops them in Crew only; kiro-cli's trust is unchanged.`,
}

const row = (state: string): ChatMessage =>
  ({
    role: 'notice',
    content: TEXT[state],
    meta: {
      kind: 'spec_hooks',
      project_hooks: state,
      project_hooks_folder: `/work/${state}`,
      // What the gateway sends for the state a successful click leads to.
      project_hooks_saved_text: state === 'off' ? TEXT.on : TEXT.off,
      project_hooks_commands: [
        { event: 'PreToolUse', when: 'before a tool runs', command: './scripts/guard.sh' },
        { event: 'PostToolUse', when: 'after a tool runs', command: 'python3 scripts/audit.py' },
      ],
    },
  }) as unknown as ChatMessage

initI18n()
createRoot(document.getElementById('root')!).render(
    <div style={{ padding: 16, maxWidth: 760, display: 'flex', flexDirection: 'column', gap: 12 }}>
      {['off', 'on', 'kiro_cli'].map((s) => (
        <div key={s} data-state={s}>
          <ProjectHooksNotice message={row(s)} askAgent={false} />
        </div>
      ))}
    </div>,
)
