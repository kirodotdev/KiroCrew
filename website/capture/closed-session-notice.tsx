/**
 * Isolated capture entry for the closed-session inline notice on a `?sid=` link.
 *
 * The transcript bubble holds the REAL `MarkdownRenderer` under a
 * `ClosedSessionCtx` whose probe fails, so a click on the link shows the inline
 * notice production shows. Three things the screenshots must make legible:
 *
 *   - the notice names the session the way the reader sees it (the link's
 *     prose text), not the raw key;
 *   - the sentence's full stop stays beside the link, and the notice follows the
 *     sentence instead of splitting it;
 *   - Try again on a session deleted since goes through the page's `open`, whose
 *     "was deleted" banner this harness paints the way the chat page's error
 *     notice would.
 *
 * Query string: ?theme=dark|light. The scene is driven from the page: click the
 * link, then the backticked key, then Try again on the link's notice. `lookup` rejects on its first call (an unreachable
 * gateway) and answers "no such session" afterwards, so Try again meets a 404.
 */
import { useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n'
import { i18nT } from '../src/i18n/t'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import { ClosedSessionCtx, type ClosedSessionActions } from '../src/components/markdown/contexts'
import ErrorNotice from '../src/components/ErrorNotice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** Synthetic roster: one open session; the linked one is closed. */
const HERE = 'chat-1-1788000000'
const CLOSED = 'chat-7-1784661951'
const ROSTER: ReadonlyMap<string, string> = new Map([[HERE, 'Packing checklist review']])

const NOTE = `The earlier attempt is in [the old run](/chat?sid=${CLOSED}). Its last message names the file to keep.\n\nThe same session as a key: \`${CLOSED}\`. Click it too.`

/** The transcript's assistant bubble, class list copied from the message row. */
const BUBBLE_CLS = 'msg-content px-4 py-3 text-sm leading-6 rounded-lg bg-bg-elevated text-text ring-1 ring-inset ring-border/60 rounded-bl-[4px] overflow-hidden min-w-0'

function Scene() {
  const [banner, setBanner] = useState<string | null>(null)
  const closed = useMemo<ClosedSessionActions>(() => {
    let probes = 0
    return {
      lookup: async () => {
        probes += 1
        if (probes === 1) throw new Error('gateway unreachable')
        return null
      },
      open: async ({ title }) => {
        setBanner(i18nT('store.chatSlice.session_gone_open_failed_named', { name: title }))
        return 'gone'
      },
    }
  }, [])
  return (
    <div className="bg-bg text-text flex flex-col min-h-screen">
      <div className="border-b border-border px-4 py-2 text-[13px] text-muted mx-auto w-full" style={{ maxWidth: 900 }}>
        Viewing session <span className="text-text font-medium">{ROSTER.get(HERE)}</span>
      </div>
      {banner && (
        <div className="mx-auto w-full px-4 pt-3" style={{ maxWidth: 900 }} data-banner>
          <ErrorNotice message={banner} onDismiss={() => setBanner(null)} />
        </div>
      )}
      <div className="px-4 pt-4 mx-auto w-full flex flex-col gap-3" style={{ maxWidth: 900 }}>
        <div className="flex flex-col items-start max-w-[80%]" data-bubble>
          <div className={BUBBLE_CLS}>
            <ClosedSessionCtx.Provider value={closed}>
              <MarkdownRenderer
                content={NOTE}
                onSessionOpen={() => {}}
                sessions={ROSTER}
                activeSession={HERE}
              />
            </ClosedSessionCtx.Provider>
          </div>
        </div>
      </div>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
