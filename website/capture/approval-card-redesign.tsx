/**
 * Isolated capture entry for the redesigned chat tool-approval card:
 *
 *   shell — a compound shell command: every command-scoped Trust row
 *   reads — a read-only command: the Read-only chip and the read-only row
 *   env   — an env-prefixed command the gateway derives no family for
 *   mcp   — an MCP tool call with arguments: no exact-call grant exists for it
 *   mcp-always — the same call where the server offers "Always allow this tool"
 *   none  — an unattended (cron) request: Trust holds its slot, disabled
 *   override — a command a deny rule refuses: the rule-override card
 *   ghost — the transcript row out of view: the card's "Show in chat" chip
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
/** A stand-in for the chat's title overlay, `overlay` px tall, for the cramped
 *  frame: the shoot script sizes it to leave the card's request less than
 *  64px of room, so the card holds Allow once and Trust and says why. */
const overlayPx = Number(params.get('overlay') || 0)

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
  // The same call on a server whose tools may be allowed for good.
  'mcp-always': {
    content: 'post_message',
    meta: {
      tool_purpose: 'Post a placeholder update to Slack',
      tool_input: JSON.stringify({ channel: '#placeholder-team', text: 'Draft is ready for review' }),
      is_shell: '', tool_kind: 'other', tool_name: 'post_message', mcp_server: 'slack-mcp',
      trust_grantable: '1', always_allow_tool_grantable: '1', always_allow_tool_ref: '@slack-mcp/post_message',
    },
  },
  // A command a deny rule refuses: the card offers the rule override instead of Trust.
  override: {
    content: 'Running: git push',
    meta: {
      tool_purpose: 'Publish the placeholder branch',
      tool_input: JSON.stringify({ command: 'git push origin main' }), is_shell: '1', is_read_only: '',
      full_command: 'git push origin main', base_command: 'git',
      deny_override_rule: 'git-publish-push-protected-branch-name', deny_override_command: 'git push origin main',
      trust_command_grantable: '1', trust_grantable: '1',
    },
  },
  // The shell scene again, with its transcript row out of view ("Show in chat").
  ghost: {
    content: 'Running: ls',
    meta: {
      tool_purpose: 'List placeholder project files',
      tool_input: JSON.stringify({ command: READS }), is_shell: '1', is_read_only: '1',
      full_command: READS, base_command: 'ls',
      trust_command_grantable: '1', trust_base_grantable: '1', trust_grantable: '1',
    },
  },
  // A browser call whose command grant is a SITE grant (#39990): `full_command`
  // is the host, and the card keeps the plain, session-only Trust menu.
  site: {
    content: 'Drive browser',
    meta: {
      tool_purpose: 'Click the sign-in button on the placeholder site',
      tool_input: JSON.stringify({ op: 'click', args: { ref: 'e3' } }), is_shell: '', is_read_only: '',
      tool_title: 'Drive browser', full_command: 'www.example.com', browser_trust_class: 'act',
      trust_command_grantable: '1', trust_grantable: '1',
    },
  },
  // A description longer than the heading's two lines, so it clamps.
  'long-purpose': {
    content: 'Running: find, head',
    meta: {
      tool_purpose: 'Find every agent-platform file under the workspace and the crew folder, skipping node_modules, so the next step can compare the two copies and report which placeholder settings differ between them',
      tool_input: JSON.stringify({ command: SHELL }), is_shell: '1', is_read_only: '',
      full_command: SHELL, base_command: 'find,head',
      trust_command_grantable: '1', trust_base_grantable: '1', trust_grantable: '1',
    },
  },
  // Long identities with no break in them: an MCP server and tool name, and a
  // shell executable whose family row names it. The 320px check runs both.
  'long-mcp': {
    content: 'create_or_update_release_notes_for_repository',
    meta: {
      tool_purpose: 'Draft the placeholder release notes',
      tool_input: JSON.stringify({ repository: 'placeholder/repo', tag: 'v0.0.1' }),
      is_shell: '', tool_kind: 'other', tool_name: 'create_or_update_release_notes_for_repository', mcp_server: 'repository_automation_and_release_management',
      trust_grantable: '1',
    },
  },
  'long-base': {
    content: 'Running: repository_automation_and_release_management',
    meta: {
      tool_purpose: 'Show the placeholder tool help',
      tool_input: JSON.stringify({ command: '/work/bin/repository_automation_and_release_management --help' }), is_shell: '1', is_read_only: '',
      full_command: '/work/bin/repository_automation_and_release_management --help', base_command: '/work/bin/repository_automation_and_release_management',
      trust_command_grantable: '1', trust_base_grantable: '1', trust_grantable: '1',
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
      {overlayPx > 0 && (
        <div data-transcript-top-overlay className="fixed inset-x-0 top-0 z-[45] flex items-end px-4 pb-2 bg-bg-elevated border-b border-border text-[13px] text-muted" style={{ height: overlayPx }}>
          Chat title (overlay)
        </div>
      )}
      <div className="flex flex-col gap-2 px-3 pb-3 overflow-hidden">
        <div className="self-end max-w-[80%] rounded-xl bg-accent-subtle px-3 py-2 text-[13px]">
          Placeholder request from the user.
        </div>
        {/* Stand-in for the transcript's inline tool pill: registering a
            visible node keeps the approval bar in its inline (non-ghost)
            form, which is the state the mockup draws. */}
        <div
          className="self-start text-[13px] font-mono text-muted"
          ref={el => { if (el && scene !== 'ghost') registerToolPill('tc-capture-1', el) }}
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

// ?lang=de draws the card in another catalog (the 320px check runs each language).
initI18n(params.get('lang') || 'en')
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
