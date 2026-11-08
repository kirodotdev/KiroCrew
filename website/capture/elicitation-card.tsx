/**
 * Isolated capture entry for the MCP elicitation card (ElicitationCard).
 *
 * WHY ISOLATED: a pending elicitation exists only while an MCP server the agent
 * called is waiting on the person mid tool call, which no capture run has. This
 * mounts the REAL card against the real stylesheet, theme tokens and live i18n
 * catalog, feeding it the row meta the chat runner appends (the normalised
 * `fields` of a form ask, or the `url` of a URL ask).
 *
 * Scene comes from the query string: ?scene=form|url|resolved|declined|cancelled&theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import ElicitationCard from '../src/pages/chat/ElicitationCard'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'form'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// `?required=reason` makes the free-text field required too, so the capture
// of the required-field hint has a field a plain Submit leaves empty (an
// unchecked required boolean is a valid `false`, not a miss).
const reasonRequired = params.get('required') === 'reason'
const fields = [
  { name: 'confirm', kind: 'boolean', required: true, title: 'Confirm deletion', description: 'The item is removed for everyone who can see it.' },
  { name: 'reason', kind: 'string', required: reasonRequired, title: 'Reason', max_length: 80 },
  { name: 'retention_days', kind: 'integer', required: false, title: 'Keep a backup for (days)', minimum: 0, maximum: 30, default: 7 },
  { name: 'visibility', kind: 'enum', required: false, title: 'Who can still see the trash entry', options: [{ value: 'owner', label: 'Only me' }, { value: 'team', label: 'My team' }] },
  { name: 'notify', kind: 'multi_enum', required: false, title: 'Notify', options: [{ value: 'owner', label: 'Owner' }, { value: 'watchers', label: 'Watchers' }] },
]

const base = {
  approval_id: 'toolu_01', tool_call_id: 'toolu_01', mid: 'row-capture-1',
  // The server and tool the ask belongs to, as the runner stamps them.
  mcp_server: 'probe', tool_name: 'delete_file',
  message: 'Really delete "Q3 launch plan" from the shared workspace? This cannot be undone.',
}
const metaByScene: Record<string, Record<string, unknown>> = {
  form: { ...base, mode: 'form', fields },
  url: { ...base, mode: 'url', message: 'Confirm the deletion in your browser to continue.', url: 'https://example.com/confirm/8f3a' },
  resolved: { ...base, mode: 'form', fields, resolved: 'accept', answer: { confirm: true, retention_days: 7, visibility: 'team' } },
  declined: { ...base, mode: 'form', fields, resolved: 'decline' },
  cancelled: { ...base, mode: 'url', message: 'Confirm the deletion in your browser to continue.', url: 'https://example.com/confirm/8f3a', resolved: 'cancel' },
}
const message: ChatMessage = { role: 'elicitation', content: String(base.message), cls: '', meta: metaByScene[scene] ?? metaByScene.form }

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <MemoryRouter>
      <div data-capture-root style={{ width: 620, margin: '16px auto', background: 'var(--bg)', padding: 16 }}>
        <ElicitationCard message={message} slot="dashboard:main" />
      </div>
    </MemoryRouter>
  </Provider>,
)
