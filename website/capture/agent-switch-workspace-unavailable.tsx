/* Evidence for the agent-switch workspace-unavailable notice.
 *
 * The 503 this change added answers a switch whose configured workspace root cannot be
 * resolved. The keyboard cycles have no disabled state, so this toast is the only place that
 * refusal is reported -- and it had no capture scene in the set this PR shipped.
 *
 * Mounts the REAL `AgentSwitchNotice` the app renders, with the copy resolved through the
 * REAL `agentSwitchFailureMessage` from a REAL `ApiError`, so the frame cannot drift from
 * either the markup or the mapping.
 *
 *   ?theme=dark|light&case=workspace|turn
 */
import { createRoot } from 'react-dom/client'

import AgentSwitchNotice from '../src/components/AgentSwitchNotice'
import { agentSwitchFailureMessage } from '../src/utils/agentSwitchFeedback'
import { ApiError } from '../src/api/client'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const which = params.get('case') === 'turn' ? 'turn' : 'workspace'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

initI18n('en')

// Both refusals a switch can answer, so the frames show this one is NOT the turn-in-flight
// copy: the two arrive on the same surface and only the code tells them apart.
const ERRORS: Record<string, ApiError> = {
  workspace: new ApiError(
    503,
    'the configured workspace directory is unavailable',
    JSON.stringify({
      error: 'the configured workspace directory is unavailable',
      code: 'workspace_unavailable',
    }),
  ),
  turn: new ApiError(
    409,
    'conflict',
    JSON.stringify({ error: 'a turn is in flight', code: 'turn_in_flight' }),
  ),
}

const WIRE: Record<string, string> = {
  workspace: 'POST /api/chat/slots/chat-1/agent -> 503 {"error":"...","code":"workspace_unavailable"}',
  turn: 'POST /api/chat/slots/chat-1/agent -> 409 {"error":"...","code":"turn_in_flight"}',
}

const message = agentSwitchFailureMessage(ERRORS[which])

function Scene() {
  return (
    <div data-capture-root className="bg-bg text-text p-5 w-[720px] h-[150px] relative">
      <div className="text-[11px] text-muted font-mono break-all">{WIRE[which]}</div>
      {/* The toast is `position: fixed`, so it anchors to the viewport rather than to this
        * wrapper -- the screenshot is taken of the page, not of the wrapper. */}
      <AgentSwitchNotice message={message} onDismiss={() => undefined} />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Scene />)
