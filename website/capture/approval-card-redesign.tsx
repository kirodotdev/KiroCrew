/**
 * Isolated capture entry for the redesigned chat tool-approval card:
 *
 *   shell — a compound shell command: every command-scoped Trust row
 *   reads — a read-only command: the Read-only chip and the read-only row
 *   env   — an env-prefixed command the gateway derives no family for
 *   mcp   — an MCP tool call with arguments: no exact-call grant exists for it
 *   none  — an unattended (cron) request: Trust holds its slot, disabled
 *
 * WHY ISOLATED: the pending-approval state only exists inside a live chat
 * session with an agent parked on an approval. This mounts the REAL ChatInput
 * against the real stylesheet, theme tokens and live i18n catalog, seeding the
 * real store with the permission row the gateway emits for each case. The
 * metadata per scene is what `permission_prompt.py` sends, so what a scene
 * offers is what the app offers: nothing here forces a menu row on or off.
 *
 * Scene and theme come from the query string: ?scene=shell&theme=dracula-light
 * The menus and the receipt are driven by the shoot script clicking the REAL
 * controls (capture/shoot-approval-card-redesign.mjs).
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatInput from '../src/components/ChatInput'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { setActiveSlot, sseChatMessage } from '../src/store/chatSlice'
import { registerToolPill } from '../src/store/toolPillRegistry'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'kiro-dark'
const scene = params.get('scene') || 'shell'

document.documentElement.setAttribute('data-theme', theme)

const SHELL = 'find /Users/dev/.kiro/crew/workspace -maxdepth 4 -iname "*agent-platform*" 2>/dev/null; find /Users/dev/.kiro/crew -maxdepth 3 -iname "*agent-platform*" -not -path "*/node_modules/*" 2>/dev/null | head'
const READS = 'ls -la /Users/dev/.kiro/crew/workspace/placeholder-project'
const ENV = 'AWS_PROFILE=readonly aws s3 ls s3://placeholder-bucket/reports/'

/** One permission row per scene, shaped as the gateway shapes it. */
const SCENES: Record<string, { content: string; meta: Record<string, unknown> }> = {
  shell: {
    content: 'Running: find, head',
    meta: {
      tool_purpose: 'Find agent-platform files in the workspace',
      tool_input: JSON.stringify({ command: SHELL }), is_shell: '1', is_read_only: '',
      full_command: SHELL, base_command: 'find,head',
      trust_command_grantable: '1', trust_base_grantable: '1', trust_grantable: '1',
    },
  },
  reads: {
    content: 'Running: ls',
    meta: {
      tool_purpose: 'List placeholder project files',
      tool_input: JSON.stringify({ command: READS }), is_shell: '1', is_read_only: '1',
      full_command: READS, base_command: 'ls',
      trust_command_grantable: '1', trust_base_grantable: '1', trust_grantable: '1',
    },
  },
  // An env-prefixed command: grantable as an exact string, with no family.
  env: {
    content: 'Running: aws',
    meta: {
      tool_purpose: 'List placeholder report files',
      tool_input: JSON.stringify({ command: ENV }), is_shell: '1', is_read_only: '',
      full_command: ENV, trust_command_grantable: '1', trust_grantable: '1',
    },
  },
  // A tool call with arguments has no exact-call grant on the server, so the
  // card carries no `full_command` and only the session-wide grant is offered.
  mcp: {
    content: 'post_message',
    meta: {
      tool_purpose: 'Post a placeholder update to Slack',
      tool_input: JSON.stringify({ channel: '#placeholder-team', text: 'Draft is ready for review' }),
      is_shell: '', tool_kind: 'other', tool_name: 'post_message', mcp_server: 'slack-mcp',
      trust_grantable: '1',
    },
  },
  // An unattended source: no standing grant can be recorded for it.
  none: {
    content: '[cron] Running: ls',
    meta: {
      source: 'cron',
      tool_purpose: 'List placeholder project files',
      tool_input: JSON.stringify({ command: READS }), is_shell: '1', is_read_only: '1',
      full_command: READS, base_command: 'ls',
      trust_command_grantable: '1', trust_base_grantable: '1', trust_grantable: '1',
    },
  },
}

const seed = SCENES[scene] ?? SCENES.shell

// The same shape the gateway's WS frames produce: an unresolved permission
// message on the active slot.
store.dispatch(setActiveSlot('capture-slot'))
store.dispatch(sseChatMessage({
  slot: 'capture-slot',
  role: 'permission',
  content: seed.content,
  meta: {
    approval_id: 'ap-capture-1', mid: 'mid-capture-1', request_id: 'req-capture-1',
    tool_title: seed.content, tool_call_id: 'tc-capture-1', rejection_reason_accepted: '1',
    ...seed.meta,
  },
}))

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  const [value, setValue] = useState('')
  return (
    <div className="flex flex-col justify-end h-screen bg-bg text-text" data-capture-root>
      <div className="flex flex-col gap-2 px-3 pb-3 overflow-hidden">
        <div className="self-end max-w-[80%] rounded-xl bg-accent-subtle px-3 py-2 text-[13px]">
          Placeholder request from the user.
        </div>
        {/* Stand-in for the transcript's inline tool pill: registering a
            visible node keeps the approval bar in its inline (non-ghost)
            form, which is the state the mockup draws. */}
        <div
          className="self-start text-[13px] font-mono text-muted"
          ref={el => { if (el) registerToolPill('tc-capture-1', el) }}
        >
          ▶ {seed.content}
        </div>
      </div>
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={() => setValue('')}
        connected
        approvalMode="normal"
      />
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Harness />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
document.documentElement.setAttribute('data-capture-scene', scene)
