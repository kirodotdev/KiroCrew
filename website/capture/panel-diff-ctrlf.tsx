/** Isolated capture entry for Ctrl+F over an edit-mode diff in the side panel (#30319).
 *
 * Mounts the REAL `MarkdownPanel` for a modified code file in diff mode (the
 * editor with a diff baseline) over a fetch stub at the API boundary. Beside it
 * sits a stand-in for chat find: a bubble-phase `document` keydown listener with
 * the same rule as `useMessageSearch` (Ctrl/Cmd+F that nothing preventDefault()ed),
 * which shows a "Find in chat" bar when it answers the chord.
 *
 * Query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { useEffect, useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClient } from '../src/api/queryClient'
import { Provider } from 'react-redux'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import MarkdownPanel from '../src/components/MarkdownPanel'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const ORIGINAL = ['def total(items):', '    s = 0', '    for i in items:', '        s += i', '    return s', ''].join('\n')
const CURRENT = ['def total(items):', '    return sum(items)', ''].join('\n')
const DIFF = '@@ -1,5 +1,2 @@\n def total(items):\n-    s = 0\n-    for i in items:\n-        s += i\n-    return s\n+    return sum(items)\n'

const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  const body = url.includes('/api/file-diff')
    ? { diff: DIFF, original: ORIGINAL, status: 'modified' }
    : url.includes('/api/knowledge/sources') ? [] : {}
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

function ChatFindStandIn() {
  const [open, setOpen] = useState(false)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f' && !e.defaultPrevented) {
        e.preventDefault()
        setOpen(true)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])
  return (
    <div style={{ flex: 1, minWidth: 0, padding: 12, borderRight: '1px solid var(--border)' }}>
      <div style={{ fontSize: 10, letterSpacing: 1, textTransform: 'uppercase', color: 'var(--text-muted)' }}>
        Chat (stand-in for chat find)
      </div>
      {open ? (
        <div data-chat-find style={{ marginTop: 8, padding: '6px 10px', border: '1px solid var(--accent)', borderRadius: 6 }}>
          Find in chat… Type to search this conversation
        </div>
      ) : (
        <div style={{ marginTop: 8, color: 'var(--text-muted)' }}>chat find closed</div>
      )}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter>
          <div data-capture-root style={{ display: 'flex', height: '100vh', background: 'var(--bg)', color: 'var(--text)', font: '13px system-ui, sans-serif' }}>
            <ChatFindStandIn />
            <div style={{ width: 620, display: 'flex', flexDirection: 'column' }}>
              <MarkdownPanel
                embedded
                active
                onContentChange={() => {}}
                onSave={async () => {}}
                onClose={() => {}}
                filePath="/repo/app.py"
                content={CURRENT}
                initialDiffMode
              />
            </div>
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
