/**
 * Evidence for structured provider notices (PR #15111).
 *
 * THE SCENE. One chat column with a provider notice at each severity the
 * adapters send -- info, warning, error -- plus an unknown future severity,
 * which must render as info. Every row goes through the REAL `notice` entry of
 * `defaultMessageRenderers` with the stored message shape
 * (`meta.kind = 'provider_notice'`), the same path live and reloaded history
 * take. A gateway-authored ⚠️ notice sits last for comparison. Info, warning and
 * unknown render as NoticeCard rows; error renders as the transcript ErrorCard,
 * the row a `role: 'error'` message takes.
 *
 *   ?theme=dark|light   ?lang=en|zh-CN
 */
import { createRoot } from 'react-dom/client'
import type { ReactNode } from 'react'

import { initI18n } from '../src/i18n/all'
import { defaultMessageRenderers, type MessageRenderContext } from '../src/app-sdk/messageRenderers'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
initI18n(params.get('lang') || 'en')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const notice = (content: string, severity?: string): ChatMessage =>
  ({
    role: 'notice',
    content,
    cls: 'msg msg-info',
    ts: '',
    meta: severity === undefined ? {} : { kind: 'provider_notice', severity },
  }) as ChatMessage

const ROWS: Array<[string, ChatMessage]> = [
  ['info', notice('UserPromptSubmit says: probe-notice: hook system message', 'info')],
  ['warning', notice('Model fallback\nclaude-opus-5-5 is at capacity; this turn used claude-sonnet-5-5.', 'warning')],
  ['error', notice('Background task failed\nThe dev server exited with code 1.', 'error')],
  ['unknown (future-severity)', notice('Provider reported a new kind of notice', 'future-severity')],
  ['gateway ⚠️ notice (existing)', notice('⚠️ Dropped a queued message: the session was reset.')],
]

const entry = defaultMessageRenderers.find(r => r.id === 'notice')!
const ctx = { row: (node: ReactNode) => node } as unknown as MessageRenderContext

// Inline styles for the frame only: capture/ is outside Tailwind's scan glob,
// so a class used only here would render unstyled. The rows themselves are the
// real components, whose classes live under src/.
createRoot(document.getElementById('root')!).render(
  <div
    data-capture-root
    style={{ maxWidth: 640, margin: '0 auto', padding: '20px 24px 28px', background: 'var(--bg)', color: 'var(--text)' }}
  >
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {ROWS.map(([label, message]) => (
        <div key={label} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <div style={{ fontSize: 11, letterSpacing: '0.04em', textTransform: 'uppercase', opacity: 0.6 }}>{label}</div>
          {entry.render(message, ctx)}
        </div>
      ))}
    </div>
  </div>,
)
