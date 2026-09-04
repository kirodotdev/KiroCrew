/**
 * Evidence for the channel approval card's trust tiers (issue #5231).
 *
 * THE PROBLEM: channel approval cards passed the agent ROLE as the card
 * title, so the TrustDropdown offered to trust a pattern like "dev" — and the
 * channel endpoint rejected trust_command / trust_base outright with
 * 400 "invalid action". Two of the three offered tiers could never succeed.
 *
 * The scene mounts the REAL ApprovalCard (which renders the REAL
 * TrustDropdown) with its title resolved by the REAL `approvalToolTitle`
 * helper from a backend-shaped approval message, against the real stylesheet,
 * theme tokens and live i18n catalog. Reaching this state in the shell needs
 * a live channel with an agent parked on a tool call; nothing here
 * re-implements the component, its classes, or its strings.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import ApprovalCard from '../src/components/ApprovalCard'
import { approvalToolTitle } from '../src/pages/ChannelPage'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

/** A channel approval message exactly as `_stream_task` posts it. */
const CONTENT =
  '⚠️ Approval needed: **Running: ls -la /workplace/project**\n```\n{"command": "ls -la /workplace/project"}\n```'
const FROM_ROLE = 'dev'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

initI18n('en')

// Same title resolution MessageBubble applies: embedded tool name first,
// agent role only as the legacy fallback.
const title = approvalToolTitle(CONTENT) || FROM_ROLE
const toolInput = CONTENT.replace(/^⚠️ Approval needed:.*\n```\n?/, '').replace(/\n?```$/, '')

createRoot(document.getElementById('root')!).render(
  <div data-capture-root className="bg-bg text-text p-5 w-[720px] flex flex-col gap-3">
    {/* Harness chrome: names the agent whose approval card is under test. */}
    <div className="text-[11px] text-muted font-mono break-all">
      <span className="not-italic text-subtle">channel agent @{FROM_ROLE} wants to run: </span>
      ls -la /workplace/project
    </div>
    <ApprovalCard
      title={title}
      toolInput={toolInput}
      showButtons
      trustAllLabelKey="components.trustDropdown.trust_all_tools_channel"
      onApprove={() => Promise.resolve()}
    />
  </div>,
)
