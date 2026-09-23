/**
 * Evidence for the message font size setting governing the whole conversation
 * surface, not just bubble prose.
 *
 * Two panels render the SAME conversation fragment, each under its own
 * `--mc-message-font-size` (14px default above, a larger setting below) and at
 * the Compact content width `scaleContentWidth()` derives from it, so the
 * widened column is part of the picture rather than a caption.
 * Every element inside is the REAL component the chat renders: an assistant
 * bubble (`.mc-message-font-scope.msg-content` around MarkdownRenderer, the
 * same wrapper AssistantMessage uses) with prose, inline code, a code block, a
 * table and a path chip; the REAL FollowUpBar; the REAL LexicalComposerInput
 * with INPUT_TYPO. Nothing in this file sets a font size by hand — the only
 * input that differs between the columns is the var, so what you see on the
 * right is exactly what the setting does.
 *
 *   ?theme=dark|light   ?size=<px>   ?compare=1 (side by side, default) | 0 (single column)
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'

import FollowUpBar from '../src/components/FollowUpBar'
import LexicalComposerInput from '../src/components/LexicalComposerInput'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import { initI18n } from '../src/i18n/all'
import { CONTENT_WIDTH } from '../src/pages/chat/ChatSettings'
import { DEFAULT_MESSAGE_FONT_SIZE, scaleContentWidth } from '../src/pages/chat/contentWidth'
import { store } from '../src/store'
import '../src/index.css'
import '../src/styles/message-font-size.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') || 'en')

const LARGE = Number(params.get('size') || 20)
const COMPARE = params.get('compare') !== '0'
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const MARKDOWN = `Not a bug — it was deliberately excluded. The composer's size comes from one shared constant, \`INPUT_TYPO\`, not from the CSS var, and it is shared by five surfaces that must stay metric-identical.

\`\`\`ts
export const INPUT_TYPO = 'px-4 pt-3 pb-1 text-sm font-body leading-normal'
\`\`\`

| Element | Today | Change |
|---|---|---|
| Inline code | pinned \`13px\` | \`0.9286em\` |
| Composer | \`text-sm\` | setting var |

See \`/Volumes/workplace/KiroCrew/website/src/components/PasteHighlightLayer.tsx\` line 7 and [PR #12665](https://github.com/kirodotdev/KiroCrew/pull/12665).`

const FOLLOW_UPS = ['Extend messageFontSize to the composer', 'Add a separate composer font-size setting', 'Just show me the diff']

function Column({ size, label }: { size: number; label: string }) {
  const width = scaleContentWidth(CONTENT_WIDTH.compact, 'compact', size)
  return (
    <section
      data-size={size}
      data-compact-width={width.messages}
      className="flex flex-col gap-3 bg-bg text-text p-4 rounded-lg border border-border"
      style={{ '--mc-message-font-size': `${size}px`, '--mc-content-width': width.messages, '--mc-input-width': width.input, width: `calc(${width.messages} + 2rem)` } as React.CSSProperties}
    >
      <div style={{ fontSize: 11, letterSpacing: '0.08em', textTransform: 'uppercase', opacity: 0.55, fontFamily: 'ui-sans-serif, system-ui, sans-serif' }}>
        {label} · {size}px · compact column {width.messages}
      </div>
      <div
        className="message-bubble mc-message-font-scope msg-content leading-relaxed text-text overflow-hidden"
        style={{ overflowWrap: 'anywhere', wordBreak: 'break-word', fontSize: 'var(--mc-message-font-size, 14px)' }}
      >
        <MarkdownRenderer content={MARKDOWN} />
      </div>
      <FollowUpBar options={FOLLOW_UPS} picked={new Set()} onSelect={() => {}} layout="singleline" />
      <div className="rounded-xl border border-border bg-card">
        <LexicalComposerInput
          value="i see that this input box still has a small font size too"
          blocks={[]}
          onChange={() => {}}
          onBlocksChange={() => {}}
          onSend={() => {}}
          ariaLabel="Message input"
          placeholder="Write a message"
        />
      </div>
    </section>
  )
}

function App() {
  return (
    <div data-capture-root className="inline-flex flex-col items-start gap-4 p-4 bg-bg">
      {COMPARE && <Column size={DEFAULT_MESSAGE_FONT_SIZE} label="default" />}
      <Column size={LARGE} label={COMPARE ? 'setting' : 'default'} />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <App />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
