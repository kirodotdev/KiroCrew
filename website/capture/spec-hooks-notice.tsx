/**
 * Evidence for the spec-hooks notice rows in Sessions chat.
 *
 * Three rows:
 * 1. run-nowhere: a session on claude or codex (backends with no hook channel)
 *    whose agent spec carries hooks — the row names the events that won't run.
 * 2. withheld: a goose or opencode session whose CHECKOUT spec sets hooks that
 *    Crew withholds, while the user-level spec of that name carries hooks that run.
 * 3. withheld-none: the same, with no user-level spec (or one without hooks), so
 *    the row names where a trusted hook can go instead of saying any still run.
 *
 * All rows carry meta.kind = "spec_hooks": drawn in Sessions chat, dropped in
 * crewmate chat (CREWMATE_HIDDEN_NOTICE_KINDS), which draws no runtime notice.
 *
 * Scenes render the real NoticeCard in both dark and light themes.
 *
 *   ?scene=run-nowhere | withheld | withheld-none
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import NoticeCard from '../src/pages/chat/NoticeCard'
import '../src/index.css'

type Scene = 'run-nowhere' | 'withheld' | 'withheld-none'

const params = new URLSearchParams(location.search)
const sceneParam = params.get('scene')
const scene: Scene = sceneParam === 'withheld' || sceneParam === 'withheld-none' ? sceneParam : 'run-nowhere'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n()

// Each string is the backend's own wording (dashboard/chat_turn/tool_approval.py).
const WITHHELD_LEAD =
  'ℹ Project hooks for agent my-agent do not run in this session: Crew does not run ' +
  'commands from a project checkout.'

const CONTENT: Record<Scene, string> = {
  'run-nowhere':
    'ℹ Agent my-agent has hooks on userPromptSubmit, stop. This backend does not run ' +
    'an agent\'s hooks, so they have no effect in this session.',
  withheld: `${WITHHELD_LEAD} The hooks in your user-level spec for agent my-agent still run.`,
  'withheld-none': `${WITHHELD_LEAD} To run a hook you trust, add it to your user-level spec for agent my-agent.`,
}

const TITLE: Record<Scene, string> = {
  'run-nowhere': 'Spec hooks run-nowhere notice (claude / codex, no hook channel)',
  withheld: 'Withheld project-spec hooks notice (goose / opencode, user-level spec has hooks)',
  'withheld-none': 'Withheld project-spec hooks notice (goose / opencode, no user-level hooks)',
}

const root = document.getElementById('root')!
createRoot(root).render(
  <div className="p-4 max-w-2xl space-y-4">
    <p className="text-xs text-muted font-mono">{TITLE[scene]}</p>
    <NoticeCard content={CONTENT[scene]} />
    <p className="text-xs text-muted">
      This row shows in Sessions chat; it is hidden in crewmate chat (CREWMATE_HIDDEN_NOTICE_KINDS).
    </p>
  </div>,
)
