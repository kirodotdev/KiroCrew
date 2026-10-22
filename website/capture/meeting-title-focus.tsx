/**
 * Isolated capture entry for the meeting-title rename focus restore (#22901).
 *
 * WHY THIS FRAME: on main, closing the inline rename editor (Enter saves,
 * Escape cancels) left focus on `document.body` because the trigger carried no
 * ref. The fix hands focus back to the title trigger, so a keyboard user keeps
 * their place. That is a VISIBLE change — the `focus-ring` on the trigger —
 * which a screenshot can show and a unit test only asserts. The shoot script
 * drives the real component (open editor, press Enter / Escape) and captures
 * the ring back on the title.
 *
 * WHY ISOLATED: the component lives deep inside a live meeting page; the real
 * component is imported unmodified and only its props are fixtures, following
 * website/capture/a11y-label-swap.tsx. `theme` comes from the query string.
 */
import { createRoot } from 'react-dom/client'
import { useState } from 'react'

import MeetingTitle from '../src/apps/meetings/components/MeetingTitle'
// The trigger's fallback label and the editor's aria-label ARE catalog
// strings, so an uninitialised i18n would render them empty.
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const theme = new URLSearchParams(location.search).get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme)

function Fixture() {
  // The parent owns the title; rename resolves after a tick and the parent
  // would normally pass the new title back. Mirror that so the saved frame
  // reads naturally.
  const [title, setTitle] = useState('Weekly standup')
  const onRename = (next: string) => {
    setTitle(next)
    return Promise.resolve()
  }
  return (
    <div
      style={{
        minHeight: '100vh',
        padding: '24px 28px',
        background: 'var(--bg)',
        color: 'var(--text)',
      }}
    >
      {/* A slice of the meeting header so the frame reads as the real surface. */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, maxWidth: 520 }}>
        <MeetingTitle title={title} onRename={onRename} />
      </div>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Fixture />)
