/**
 * Isolated capture entry for a reply's post-stream reveal: the real
 * AssistantMessage, whose final chunk carries a whole widget and ends the turn
 * in the same render, so the smooth reveal is still drawing the widget body
 * after `isStreaming` turns false. `shoot-assistant-widget-drain.mjs` counts the
 * sandbox documents that widget mints and shoots the settled result.
 */
import { createRoot } from 'react-dom/client'
import { useEffect, useState } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import AssistantMessage from '../src/pages/chat/AssistantMessage'
import { ThemeProvider } from '../src/hooks/useTheme'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

document.documentElement.setAttribute('data-theme', 'kiro-dark')
const rows = Array.from({ length: 60 }, (_, i) => `<tr><td>step ${i}</td><td>${'value '.repeat(4)}</td></tr>`).join('\n')
const WIDGET = `<mcwidget title="Fix diagram">\n<style>.f{font:12px system-ui;padding:8px}</style>\n<div class="f"><h3>Diagram</h3><table>\n${rows}\n</table></div>\n</mcwidget>`
const INTRO = 'The fix adds two checks. The diagram below shows them.\n\n'
const FULL = `${INTRO}${WIDGET}\n\nThat is the whole change.`
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  const [content, setContent] = useState(INTRO)
  const [streaming, setStreaming] = useState(true)
  useEffect(() => {
    // The rest of the reply arrives in one chunk and the turn ends with it.
    const t = setTimeout(() => { setContent(FULL); setStreaming(false) }, 1500)
    return () => clearTimeout(t)
  }, [])
  return <AssistantMessage content={content} isStreaming={streaming} messageTs="2026-11-24T10:00:00.000000+00:00" slotKey="chat-widget-drain" showFooter={false} />
}

async function main() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <MemoryRouter>
            <div className="h-screen overflow-auto bg-bg text-text p-4" data-capture-root><Harness /></div>
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}
main()
