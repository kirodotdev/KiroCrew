/**
 * Isolated capture entry for the Spec Builder DECISIONS card.
 *
 * WHY ISOLATED: the card only appears inside a selected spec whose agent has
 * written `.spec-state.json`, which needs a booted gateway, a project directory
 * and a live worker slot. A half-stubbed SPA shell renders its error boundary
 * instead of the panel, which is worse evidence than none — so this mounts the
 * real SpecStatePanel against the real stylesheet and theme variables.
 *
 * The states photographed are the ones the change is about:
 *   pending — options offered, the only state in which a click is accepted
 *   locked  — the same decision after its answer went to the agent, INCLUDING
 *             the case where the agent's own state file re-emits it as pending
 *   clicked — the optimistic lock, before any refetch or agent write
 *
 * Scene + theme come from the query string: ?scene=locked&theme=dark
 */
import { createRoot } from 'react-dom/client'

// Initialise i18next exactly as main.tsx does; without the call every label in
// the frame is blank, which silently misrepresents the real UI.
import { initI18n } from '../src/i18n'
import SpecStatePanel from '../src/apps/spec-builder/components/SpecStatePanel'
import { type SpecDetail } from '../src/apps/spec-builder/api'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'pending'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const DECISION = {
  id: 'transport',
  title: 'Inbound transport',
  options: ['Hosted HTTPS listener', 'Bot Framework Streaming Extensions'],
  recommended: 'Hosted HTTPS listener',
}

const PENDING: SpecDetail = {
  name: 'webex-bridge',
  state: {
    decisions: [DECISION],
    blocking: 'Drafting requirements.md as soon as this decision is answered.',
  },
  context: { turns: 4, tool_calls: 11, worktree_branch: 'spec/webex-bridge' },
}

/** The re-emitted card: the agent wrote `answer: null` again, and the backend
 *  overlaid its own record onto it. Pre-fix this rendered clickable options. */
const LOCKED: SpecDetail = {
  name: 'webex-bridge',
  state: {
    decisions: [{ ...DECISION, answer: 'Hosted HTTPS listener', locked: true }],
    blocking: 'Drafting requirements.md.',
  },
  context: { turns: 6, tool_calls: 18, worktree_branch: 'spec/webex-bridge' },
}

const DETAIL: Record<string, SpecDetail> = {
  pending: PENDING,
  locked: LOCKED,
  clicked: PENDING,
  // Options held back while a turn is in flight: an answer sent now would be
  // queued behind it, and Pause clears that queue.
  busy: { ...PENDING, running: true },
}

// The clicked scene photographs the OPTIMISTIC lock, so the send must stay
// in flight: resolving it would let the panel move on to a state the detail
// payload does not describe.
const answerDecision = () => new Promise<void>(() => {})

function Frame() {
  return (
    <div data-capture-root style={{ width: 420, padding: 16, background: 'var(--bg)' }}>
      <SpecStatePanel detail={DETAIL[scene]} answerDecision={answerDecision} />
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Frame />)
