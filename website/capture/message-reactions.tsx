/**
 * Isolated capture entry for message reactions.
 *
 * WHY ISOLATED: no gateway is needed to show the fixed emoji row — this
 * mounts the REAL `UserMessage` and `AssistantMessage` over the literal
 * ChatPage row chain (see `quote-message.tsx` for the shared `RowBox`
 * pattern), with local state standing in for `useChatReactions`.
 *
 * Query string: ?theme=dark|light&scene=idle|active|hover|error|error-limit|error-remove|load-failed|menu-open
 *   `idle`   — the row at rest with no reaction: no chips, and the add `+`
 *              trigger collapsed to zero paint, so the message pays no height.
 *   `active` — 👍 placed on the assistant reply (a chip, visible at rest with the
 *              `+` beside it); ✅ and ⭐ placed on the user message,
 *              each drawn as its own chip (⭐ is NOT hidden behind a bare +).
 *   `hover`  — the add `+` trigger as a hover/focus reveals it (the harness
 *              forces the CSS reveal, see message-reactions.html).
 *   `error`  — the inline failure notice under the assistant reply, naming the
 *              emoji, with the add control kept visible so retry is live.
 *   `error-limit` — the per-chat reaction-limit notice under the reply.
 *   `error-remove` — the remove-failure notice under the reply; the 👍 it
 *              tried to remove is still placed, as after the hook's rollback.
 *   `load-failed` — the chat-level "Could not load reactions" notice, drawn
 *              above the transcript rows with the props ChatPage.tsx uses.
 *   `menu-open` — the add menu open: the fixed set, with "Remove …" + a check
 *              on the placed one.
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider, QueryClient } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import UserMessage from '../src/pages/chat/UserMessage'
import AssistantMessage from '../src/pages/chat/AssistantMessage'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import ErrorNotice from '../src/components/ErrorNotice'
import { i18nT } from '../src/i18n/t'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const scene = params.get('scene') || 'idle'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-mode', theme)

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const render = (c: string) => <MarkdownRenderer content={c} softBreaks />

const USER_TS = '2026-09-29T09:12:00Z'
const ASSISTANT_TS = '2026-09-29T09:12:40Z'
// Neutral sample text: it must not name on-screen affordances (an earlier
// draft mentioned "composer", "picker" and "pinning", which a reader tied to
// nothing visible — UX review). It is only filler for the reaction row.
const ASSISTANT_TEXT = [
  'The migration runs in three stages, each reversible on its own:',
  '',
  '- Stage one copies the records into the new table and leaves the old one in place.',
  '- Stage two switches reads to the new table once the row counts match.',
  '- Stage three drops the old table after a quiet period with no fallbacks.',
].join('\n')

function RowBox({ user, children }: { user?: boolean; children: React.ReactNode }) {
  return (
    <div className="px-4 mx-auto w-full py-1" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div className={`group/msg flex flex-col min-w-0 ${user ? 'items-end' : ''}`}>
        <div className={`flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full ${user ? 'items-end' : ''}`}>{children}</div>
      </div>
    </div>
  )
}

function Scene() {
  const [userReactions, setUserReactions] = useState<Set<string>>(
    // Keep the question's markers in the error scene too, so error-* and
    // active-* compare like for like (UX review note).
    new Set(scene === 'active' || scene === 'error' ? ['✅', '⭐'] : []),
  )
  const [assistantReactions, setAssistantReactions] = useState<Set<string>>(
    // A failed remove rolls back, so the 👍 it tried to take off is still placed.
    new Set(scene === 'active' || scene === 'menu-open' || scene === 'error-remove' ? ['👍'] : []),
  )
  const toggle = (set: Set<string>, setter: (s: Set<string>) => void, emoji: string) => {
    const next = new Set(set)
    if (next.has(emoji)) next.delete(emoji); else next.add(emoji)
    setter(next)
  }
  // The three per-message failure notices under the assistant reply: a failed
  // add (`error`), the per-chat limit (`error-limit`) and a failed remove
  // (`error-remove`), each with the `+` control kept visible.
  const errorKind = scene === 'error' ? 'react' as const
    : scene === 'error-limit' ? 'limit' as const
      : scene === 'error-remove' ? 'unreact' as const
        : null
  // The limit scene carries the cap the server reports in its 409 body, so the
  // notice states the number the way it does in the app.
  const assistantError = errorKind
    ? { slot: 'chat-1', mid: 'cap-assistant', emoji: '👍', kind: errorKind, ...(errorKind === 'limit' ? { limit: 500 } : {}) }
    : null
  return (
    <div className="bg-bg text-text flex flex-col" style={{ height: '100vh' }} data-capture-root data-scene={scene}>
      {/* `load-failed`: the chat-level notice ChatPage.tsx draws directly above
          ChatTranscriptView when the reactions GET has failed — the same
          component, variant, message key and className as that call site. */}
      {scene === 'load-failed' && (
        <ErrorNotice
          variant="inline"
          message={i18nT('pages.chat.messageReactionBar.reactions_load_failed')}
          className="mx-4 mt-1"
        />
      )}
      <div className="flex-1 overflow-y-auto py-4">
        <RowBox user>
          <UserMessage
            content="Can you show me the new reaction row on a reply?"
            messageTs={USER_TS}
            timestamp="09:12"
            timestampTitle="Today 09:12"
            slotKey="chat-1"
            slotTitle="Message reactions"
            renderContent={render}
            activeReactions={userReactions}
            onToggleReaction={(emoji) => toggle(userReactions, setUserReactions, emoji)}
          />
        </RowBox>
        <RowBox>
          <AssistantMessage
            content={ASSISTANT_TEXT}
            isStreaming={false}
            slotRunning={false}
            messageTs={ASSISTANT_TS}
            timestamp="09:12"
            timestampTitle="Today 09:12"
            slotKey="chat-1"
            slotTitle="Message reactions"
            activeReactions={assistantReactions}
            onToggleReaction={(emoji) => toggle(assistantReactions, setAssistantReactions, emoji)}
            reactionError={assistantError}
            onClearReactionError={() => {}}
          />
        </RowBox>
      </div>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Scene />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
