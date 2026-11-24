/**
 * Isolated capture entry for the account's shared model picker on the KAS harness.
 *
 * Two scenes, picked by `?scene=`:
 *   - `picker` (default): the REAL ModelEffortDropdown held open over the model
 *     list the KAS bundle route serves, one row marked `hiddenByAccount`
 *     (`claude-haiku-4.5`) and one hidden by Crew's own list (`glm-5`). Ids pass
 *     through filterInteractiveModels exactly as the chat does, so neither row
 *     is listed. `?active=claude-haiku-4.5` makes the account-hidden model the
 *     session's current one, which keeps it listed.
 *   - `error`: the REAL ErrorCard with the terminal wording KAS's
 *     `InvalidModelError` now gets (a model the account is not served), with
 *     the picker and Settings actions the entitlement kind offers.
 *
 * Query: ?theme=dark|light (maps to the app's kiro-dark / kiro-light tokens).
 */
import { createRef } from 'react'
import { createRoot } from 'react-dom/client'
import ModelEffortDropdown from '../src/components/ModelEffortDropdown'
import { filterInteractiveModels } from '../src/hooks/useInteractiveModels'
import { initI18n } from '../src/i18n/all'
import { ErrorCard } from '../src/pages/chat/ErrorCard'
import type { ModelInfo } from '../src/providers/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const scene = params.get('scene') || 'picker'
const active = params.get('active') || 'auto'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n('en')

// The KAS bundle route's /api/models, as the ACP adapter maps it.
const served: ModelInfo[] = [
  { name: 'auto', description: 'Models chosen by task for optimal usage and consistent quality', contextWindow: 1000000, rateMultiplier: 1 },
  { name: 'claude-opus-4.7', description: 'Claude Opus 4.7 model with 1M context window', contextWindow: 1000000, rateMultiplier: 2.2 },
  { name: 'claude-sonnet-4.6', description: 'Claude Sonnet 4.6 model with 1M context window', contextWindow: 1000000, rateMultiplier: 1 },
  { name: 'claude-haiku-4.5', description: 'The latest Claude Haiku model', contextWindow: 200000, rateMultiplier: 0.4, hiddenByAccount: true },
  { name: 'glm-5', description: 'GLM-5 model', contextWindow: 200000, rateMultiplier: 0.5 },
  { name: 'gemini-2.5-pro', description: 'Gemini 2.5 Pro model', contextWindow: 1000000, rateMultiplier: 1 },
]
const crewHidden = ['glm-5']

const ERROR_TEXT =
  "❌ The model 'claude-nonexistent-9' is not available on your account. Pick another model in the model picker " +
  'for this session, and change the default model under Settings → Chat (agent.model in ~/.kiro/crew/config.json) ' +
  "or the agent's own model if it is set to 'claude-nonexistent-9'. Retrying will not help. " +
  '(request_id: 97e3769e-e498-4d25-9066-43aea7a9ae53)'

const noop = () => {}

function Picker() {
  const anchor = { right: 400, top: 540, left: 40, bottom: 560, width: 360, height: 20 } as DOMRect
  return (
    <ModelEffortDropdown
      anchorRect={anchor}
      dropdownRef={createRef()}
      inputRef={createRef()}
      models={filterInteractiveModels(served, crewHidden, [active], false)}
      activeModel={active}
      onSelectModel={noop}
      filter=""
      setFilter={noop}
      onClose={noop}
      hasEffort={false}
      slot={null}
      currentEffort=""
      onListKeyDown={noop}
    />
  )
}

function UnservedCard() {
  return (
    <div data-episode="unserved" style={{ maxWidth: 760, display: 'flex', flexDirection: 'column' }}>
      <ErrorCard content={ERROR_TEXT} onPickModel={noop} onOpenDefaultModel={noop} />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <div
    data-capture-root
    style={{
      position: 'relative', width: scene === 'error' ? 820 : 440, height: scene === 'error' ? 230 : 580,
      padding: scene === 'error' ? 24 : 0,
      background: 'var(--bg)', color: 'var(--text)',
      font: '13px system-ui, -apple-system, sans-serif',
    }}
  >
    {scene === 'error' ? <UnservedCard /> : <Picker />}
  </div>,
)
