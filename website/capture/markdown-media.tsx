/**
 * Isolated capture entry for media referenced with markdown image syntax
 * (`![clip](demo.mp4)`).
 *
 * The transcript renders through the real MarkdownRenderer. The shoot script
 * (shoot-markdown-media.mjs) answers /api/file-stream and /api/file-raw the
 * way the backend does: the local clip streams a real MP4, the missing clip is
 * a 404, and /api/file-raw refuses a video body with 403.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n/all'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const MD = `A screenshot (control):

![Settings page](/home/user/project/shots/settings.png)

The recording of the flow:

![Demo recording](/home/user/project/shots/demo.mp4)

A clip that was cleaned up since:

![Old run](/tmp/recordings/old-run.mp4)

A clip hosted elsewhere:

![Release walkthrough](https://media.example.com/walkthrough.mp4)
`

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <div className="min-h-screen bg-bg text-text p-8" style={{ maxWidth: 720 }}>
    <MarkdownRenderer content={MD} />
  </div>,
)
