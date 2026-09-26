/**
 * Evidence for the channel approval card driven by the message's structured
 * `meta` (#5250): the server's own verdict on which trust tiers it can record.
 *
 * Mounts the REAL MessageBubble (real body, real ApprovalCard, real
 * TrustDropdown) from messages shaped exactly as `_stream_task` posts them,
 * prose AND meta. One scene per state the card can be in:
 *   simple    - `ls -la /home/dev/project`: meta grants every tier; the base
 *               tier names the server-derived binary.
 *   compound  - `cat f | wc -l`: the server derives no base, so the base tier
 *               is withheld on its word (the prose path offered "cat").
 *   nonshell  - `cron_add`: no per-command tier at all; the one control IS the
 *               blanket channel grant.
 *   legacy    - the same simple command persisted BEFORE meta existed (no meta):
 *               the prose path, unchanged.
 *   long      - a command past the server's 500-character retained-field bound:
 *               no card at all. The server refuses the request (the agent gets a
 *               rejection) and posts a bounded system notice saying why, with
 *               the same bounded excerpt the card would have shown.
 *
 *   ?theme=dark|light&scene=simple|compound|nonshell|legacy|long
 */
import { createRoot } from 'react-dom/client'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'

import { MessageBubble } from '../src/pages/ChannelPage'
import chatReducer from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import type { RootState } from '../src/store'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const scene = params.get('scene') || 'simple'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SIMPLE = 'ls -la /home/dev/project'
const COMPOUND = 'cat /home/dev/project/README.md | wc -l'
/** The server's `_APPROVAL_FIELD_MAX_CHARS`: the bound every `meta` value and
 *  the fenced input obey. Mirrored here only to shape the fixture the way the
 *  server posts it; the assertion that the two agree lives in the backend
 *  tests, not in a capture entry. */
const FIELD_MAX = 500
/** A realistic long command -- a backup with many excludes -- past the bound
 *  (528 characters). */
const LONG = [
  'tar --create --gzip --file=/home/dev/project/backups/site-2026-09-26.tar.gz',
  '--exclude=node_modules --exclude=.git --exclude=dist --exclude=coverage --exclude=.cache',
  '--exclude=*.log --exclude=*.tmp --exclude=*.pyc --exclude=__pycache__ --exclude=.venv',
  '--exclude=.pytest_cache --exclude=.mypy_cache --exclude=.ruff_cache --exclude=playwright-report',
  '--directory=/home/dev/project website/src website/public website/capture website/scripts',
  'src/kiro_crew test docs/system-specs README.md pyproject.toml package.json package-lock.json',
].join(' ')

function shell(cmd: string, base: string | null) {
  const title = `Running: ${cmd}`
  const input = JSON.stringify({ command: cmd })
  return {
    content: `⚠️ Approval needed: **${title}**\n\`\`\`\n${input}\n\`\`\``,
    meta: {
      tool_title: title, tool_input: input, command_grantable: '1',
      base_derivable: base ? '1' : '', base_command: base ?? '',
    },
  }
}

/** The system notice `_stream_task` posts instead of a card when the title
 *  would not fit the bound: same wording, same bounded excerpt. */
function refused(cmd: string) {
  const input = JSON.stringify({ command: cmd }).slice(0, FIELD_MAX)
  return {
    msgType: 'system',
    content: `⛔ Approval refused: this command is ${`Running: ${cmd}`.length} characters and a channel approval can show at most ${FIELD_MAX}. Nothing was run. A request the reader cannot read in full is not approved here; the agent can split it into shorter steps. First ${input.length} characters of the input:\n\`\`\`\n${input}\n\`\`\``,
  }
}

const SCENES: Record<string, { content: string; meta?: Record<string, string>; msgType?: string }> = {
  simple: shell(SIMPLE, 'ls'),
  compound: shell(COMPOUND, null),
  nonshell: {
    content: '⚠️ Approval needed: **cron_add**\n```\n{"name": "nightly-digest", "cron_expr": "0 9 * * *"}\n```',
    meta: {
      tool_title: 'cron_add', tool_input: '{"name": "nightly-digest", "cron_expr": "0 9 * * *"}',
      command_grantable: '', base_derivable: '', base_command: '',
    },
  },
  legacy: { content: shell(SIMPLE, 'ls').content },
  long: refused(LONG),
}
const picked = SCENES[scene] ?? SCENES.simple

await initI18n()

const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer },
  preloadedState: {
    // `normal` is the mode that renders the decision controls at all.
    dashboard: { approvalMode: 'normal' } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, messages: [], toolLog: [], slotStatusDetail: {} } as unknown as RootState['chat'],
  },
})

const msg = {
  id: 'm1',
  fromId: 'a1',
  fromRole: 'dev',
  content: picked.content,
  meta: picked.meta,
  msgType: picked.msgType ?? 'approval',
  timestamp: '2026-09-23T10:00:00Z',
  replyCount: 0,
} as unknown as Parameters<typeof MessageBubble>[0]['msg']

const agents = [{ id: 'a1', role: 'dev' }] as unknown as Parameters<typeof MessageBubble>[0]['agents']

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <div data-capture-root className="bg-bg text-text p-5 w-[760px]">
      <MessageBubble msg={msg} agents={agents} onApprove={() => Promise.resolve()} />
    </div>
  </Provider>,
)
