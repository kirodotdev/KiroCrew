/**
 * Evidence for the chat tool group's refused-approval notice.
 *
 * Mounts the REAL CollapsibleToolGroup, parked on one pending shell approval,
 * against the real stylesheet, theme tokens and i18n catalog. Its decide is
 * answered the way the server answers a press on an approval that is gone (a
 * 404), so the group shows the ErrorNotice it renders for that refusal.
 * Nothing here re-implements the group, its classes or its strings.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import CollapsibleToolGroup from '../src/pages/chat/CollapsibleToolGroup'
import { ApiError } from '../src/api/client'
import { initI18n } from '../src/i18n'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

initI18n('en')

const META = {
  approval_id: 'req-9', tool_call_id: 'tc-9', tool_kind: 'execute',
  tool_input: '{"command": "npm run deploy -- --stage staging"}',
}

createRoot(document.getElementById('root')!).render(
  <div data-capture-root className="bg-bg text-text p-5 w-[720px]">
    <CollapsibleToolGroup
      count={1}
      autoExpand
      hasPermission
      permissionMeta={META}
      onApprove={() => Promise.reject(new ApiError(404, 'no pending approval'))}
    >
      <div className="text-[12px] text-muted font-mono px-4 py-1">execute_bash</div>
    </CollapsibleToolGroup>
  </div>,
)
