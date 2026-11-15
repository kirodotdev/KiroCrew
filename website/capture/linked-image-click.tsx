/**
 * Isolated capture entry for a click on a markdown image wrapped in a link to
 * itself (`[![d](d.png)](d.png)`), as a side-panel markdown file renders it.
 *
 * The document renders through the real MarkdownRenderer under the side
 * panel's BasePathCtx, so the image resolves to /api/file-raw and its link
 * keeps the relative href and the new-tab target a file preview gives it. The
 * real Lightbox is mounted beside it. The shoot script answers /api/file-raw
 * with a PNG and counts the tabs a click opens.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n/all'
import MarkdownRenderer, { Lightbox } from '../src/components/MarkdownRenderer'
import { BasePathCtx } from '../src/components/markdown/contexts'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'light'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const MD = `# Design notes

The request path, click the diagram to enlarge it:

[![Request path](diagram.png)](diagram.png)
`

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <div className="min-h-screen bg-bg text-text p-8" style={{ maxWidth: 720 }}>
    <BasePathCtx.Provider value="/home/user/project/docs">
      <MarkdownRenderer content={MD} sourcePos />
    </BasePathCtx.Provider>
    <Lightbox />
  </div>,
)
