/**
 * Evidence for the model-entitlement error row.
 *
 * BEFORE: what a user in a partition that does not serve `auto` saw — the
 * generic entitlement wording, which recommends the very value that just
 * failed ("set agent.model to 'auto'"), on a card offering Continue, which
 * replays the identical rejection.
 *
 * AFTER: the same rejection carrying `meta.kind = model_unentitled`. The
 * wording routes to the picker and to Settings → Chat → Default Model, and the
 * card offers those two actions instead of Continue.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import { ErrorCard } from '../src/pages/chat/ErrorCard'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') || 'en')

const SERVED = 'gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, deepseek-3.2, minimax-m2.5, minimax-m2.1, glm-5, qwen3-coder-next'

const BEFORE_TEXT =
  `❌ Your account does not have access to model 'auto'. Available to you: ${SERVED}. ` +
  "Pick one in the model picker, or set agent.model to 'auto' to let the backend choose a model " +
  'your plan includes. Retrying will not help. (request_id: f1b93a64-68cc-41b4-a606-322e1533f771)'

const AFTER_TEXT =
  `❌ Your account does not have access to model 'auto' — the automatic model choice is not available on your account. ` +
  `Available to you: ${SERVED}. Pick one of these in the model picker for this session, and change the default model ` +
  "under Settings → Chat (agent.model in ~/.kiro/crew/config.json) so new sessions do not start on 'auto' again. " +
  'Retrying will not help. (request_id: f1b93a64-68cc-41b4-a606-322e1533f771)'

const PINNED_AUTO_SERVED =
  "❌ Your account does not have access to model 'claude-opus-4.8'. Available to you: gpt-5.6-sol, gpt-5.6-terra, " +
  "gpt-5.6-luna, deepseek-3.2, auto. Pick one of these in the model picker for this session, and change the default " +
  "model under Settings → Chat if it is set to 'claude-opus-4.8' — set agent.model to 'auto' in ~/.kiro/crew/config.json " +
  'to let the backend choose a model your plan includes. Retrying will not help. (request_id: 6f2d9a1b-3c4e-4d5f-8a9b-1c2d3e4f5a60)'

const PINNED_NO_AUTO =
  "❌ Your account does not have access to model 'claude-opus-4.8'. Available to you: gpt-5.6-sol, gpt-5.6-terra, " +
  'gpt-5.6-luna, deepseek-3.2, minimax-m2.5, minimax-m2.1, glm-5, qwen3-coder-next. Pick one in the model picker for this ' +
  "session, and change the default model under Settings → Chat (agent.model in ~/.kiro/crew/config.json) if it is set to " +
  "'claude-opus-4.8'. Retrying will not help. (request_id: 8a1c44d0-2e3b-4f6a-9c1d-5b7e0f2a3c99)"

const BLIP_WITH_AUTO =
  "❌ Model 'glm-5' is unavailable on the backend right now (capacity throttle or region rollout). " +
  "Try: (1) pick a different model in the model picker, (2) set agent.model to 'auto' in ~/.kiro/crew/config.json, " +
  'or (3) wait a minute and retry. (request_id: 3c0e5b2a-1f4d-4a9e-9b7c-2d6f8e1a0c44)'

const BLIP_NO_AUTO =
  "❌ Model 'glm-5' is unavailable on the backend right now (capacity throttle or region rollout). " +
  'Try: (1) pick a different model in the model picker, or (2) wait a minute and retry. ' +
  '(request_id: 3c0e5b2a-1f4d-4a9e-9b7c-2d6f8e1a0c44)'

function Label({ children }: { children: string }) {
  return (
    <div
      style={{
        fontSize: 11,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        opacity: 0.55,
        margin: '18px 0 6px',
        fontFamily: 'ui-sans-serif, system-ui, sans-serif',
      }}
    >
      {children}
    </div>
  )
}

function Scene() {
  return (
    <div
      data-capture-root
      style={{
        maxWidth: 760,
        margin: '0 auto',
        padding: '20px 24px 28px',
        background: 'var(--bg)',
        color: 'var(--text)',
      }}
    >
      <Label>BEFORE — generic wording recommends 'auto' again; Continue replays the rejection</Label>
      <div data-episode="before" style={{ display: 'flex', flexDirection: 'column' }}>
        <ErrorCard content={BEFORE_TEXT} onContinue={() => {}} />
      </div>
      <Label>AFTER — dashboard: both actions replace Continue, with the "do both" line</Label>
      <div data-episode="after" style={{ display: 'flex', flexDirection: 'column' }}>
        <ErrorCard content={AFTER_TEXT} onPickModel={() => {}} onOpenDefaultModel={() => {}} />
      </div>
      <Label>AFTER — embed / popout: no settings route here, so only the picker action</Label>
      <div data-episode="after-popout" style={{ display: 'flex', flexDirection: 'column' }}>
        <ErrorCard content={AFTER_TEXT} onPickModel={() => {}} unentitledElsewhere />
      </div>
      <Label>AFTER — pane (no picker of its own): prose only, no Continue</Label>
      <div data-episode="after-pane" style={{ display: 'flex', flexDirection: 'column' }}>
        <ErrorCard content={AFTER_TEXT} unentitledElsewhere />
      </div>
      <Label>Pinned model rejected, 'auto' served — same two-step prose, 'auto' named as the default to set</Label>
      <div data-episode="pinned-auto-served" style={{ display: 'flex', flexDirection: 'column' }}>
        <ErrorCard content={PINNED_AUTO_SERVED} onPickModel={() => {}} onOpenDefaultModel={() => {}} />
      </div>
      <Label>Pinned model rejected where 'auto' is not served either — no 'auto' recommendation</Label>
      <div data-episode="pinned-no-auto" style={{ display: 'flex', flexDirection: 'column' }}>
        <ErrorCard content={PINNED_NO_AUTO} onPickModel={() => {}} onOpenDefaultModel={() => {}} />
      </div>
      <Label>Capacity blip, 'auto' served — three remedies (unchanged wording)</Label>
      <div data-episode="blip-with-auto" style={{ display: 'flex', flexDirection: 'column' }}>
        <ErrorCard content={BLIP_WITH_AUTO} onContinue={() => {}} />
      </div>
      <Label>Capacity blip, 'auto' not served — the 'auto' step is dropped</Label>
      <div data-episode="blip-no-auto" style={{ display: 'flex', flexDirection: 'column' }}>
        <ErrorCard content={BLIP_NO_AUTO} onContinue={() => {}} />
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Scene />)
