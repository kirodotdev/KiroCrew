/**
 * Evidence for the reuse-path "Copied" flash on RunInTerminalBtn.
 *
 * THE STATE: when Run-in-terminal COPIES the command (reuse-current-terminal
 * on, an existing tab is focused and the command is placed on the clipboard
 * rather than executed) ChatPage echoes `mc:run-in-terminal-result` with
 * `{ ok: true, copied: true }`. The button then flashes the `copied` status —
 * a ClipboardCheck glyph whose title/aria-label is the reuse-specific string
 * `components.runInTerminalBtn.copied_paste_into_terminal`
 * ("Copied — paste it into the terminal"), NOT the generic markdown "Copied!".
 *
 * This mounts the REAL component against the REAL stylesheet, theme tokens and
 * live i18n catalog. Nothing here re-implements the glyph, its classes or its
 * string, so the frame proves what ships. The copied status is reached by
 * driving the component's own event contract: dispatch the result event with
 * copied=true (correlated by the reqId the component put on its request), which
 * is exactly what ChatPage does on the reuse path.
 *
 * The flash auto-reverts after 1200ms, so the harness screenshots inside that
 * window (the capture script waits for the glyph, then shoots immediately).
 *
 * Query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import RunInTerminalBtn from '../src/components/RunInTerminalBtn'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

await initI18n()

// The reuse path never executes: it copies the command to the clipboard and
// asks the user to paste it. Intercept the request the button dispatches and
// answer it as a COPY result, correlated by the button's own reqId — the same
// shape ChatPage emits when dashboard.terminal.reuse_current is on.
window.addEventListener('mc:run-in-terminal', (e: Event) => {
  const reqId = (e as CustomEvent).detail?.reqId
  window.dispatchEvent(new CustomEvent('mc:run-in-terminal-result', {
    detail: { reqId, ok: true, copied: true },
  }))
})

createRoot(document.getElementById('root')!).render(
  <div
    data-capture-root
    style={{
      width: 360,
      height: 160,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      gap: 12,
      background: 'var(--bg)',
      color: 'var(--text)',
      fontSize: 14,
    }}
  >
    <span>npm run build</span>
    <RunInTerminalBtn code="npm run build" lang="bash" />
  </div>,
)
