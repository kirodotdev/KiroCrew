/**
 * Isolated capture entry for the two in-chat approval surfaces showing a GONE
 * approval as STATUS, not an error (#18203): the chat approval card
 * (`ApprovalCard`) and the chat tool group (`CollapsibleToolGroup`).
 *
 * WHY ISOLATED: both surfaces only render inside a live chat transcript with a
 * pending tool permission, which a capture run cannot stage. This mounts the
 * REAL components against the real stylesheet, theme tokens and live i18n
 * catalog, and drives each one's own Approve press through an `onApprove` that
 * rejects with the gateway's "no pending approval" (a terminal 404) — the same
 * seam the chat page wires — so the photographed surface is the shipped one in
 * exactly the state issue #18203 is about.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import ApprovalCard from '../src/components/ApprovalCard'
import CollapsibleToolGroup from '../src/pages/chat/CollapsibleToolGroup'
import { ApiError } from '../src/api/client'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Every Approve press answers as the gateway does when the future is gone.
const refuse = () => Promise.reject(new ApiError(404, 'no pending approval'))

function Scene() {
  // Click both surfaces' Approve on mount, so the capture lands on the gone
  // state without the harness having to know each surface's button layout.
  useEffect(() => {
    const t = setTimeout(() => {
      document.querySelectorAll<HTMLButtonElement>('[data-capture-root] button').forEach((b) => {
        if (/^\s*Approve\s*$/.test(b.textContent || '')) b.click()
      })
    }, 50)
    return () => clearTimeout(t)
  }, [])
  return (
    <div data-capture-root style={{ maxWidth: 560, margin: '24px auto', display: 'flex', flexDirection: 'column', gap: 24 }}>
      <div>
        <div style={{ fontSize: 11, opacity: 0.5, marginBottom: 6 }}>ApprovalCard</div>
        <ApprovalCard title="shell" toolInput="rm -rf build && npm run build" showButtons onApprove={refuse} />
      </div>
      <div>
        <div style={{ fontSize: 11, opacity: 0.5, marginBottom: 6 }}>CollapsibleToolGroup</div>
        <CollapsibleToolGroup count={1} hasPermission onApprove={refuse}>
          <div style={{ fontSize: 12, opacity: 0.6 }}>shell · rm -rf build &amp;&amp; npm run build</div>
        </CollapsibleToolGroup>
      </div>
    </div>
  )
}

await initI18n()
createRoot(document.getElementById('root')!).render(<Scene />)
