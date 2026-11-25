/**
 * Isolated capture entry for relative links in a side-panel markdown preview
 * (issue #39492).
 *
 * The document renders through the real MarkdownRenderer under the side
 * panel's BasePathCtx with an open handler, as ContentRenderer wires it. The
 * shoot script answers POST /api/file-kinds: `sibling.md` exists and
 * `gone.md` does not. A click that reaches the open handler is written into
 * the "Opened" line so the frame shows where it went.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n/all'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import { BasePathCtx } from '../src/components/markdown/contexts'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'light'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const MD = `# Spec index

- Next step: [the sibling note](sibling.md)
- Old draft: [the removed note](gone.md)
`

function Harness() {
  const [opened, setOpened] = useState('nothing yet')
  return (
    <div className="min-h-screen bg-bg text-text p-8" style={{ maxWidth: 720 }}>
      <BasePathCtx.Provider value="/home/user/project/spec/index.md">
        <MarkdownRenderer content={MD} sourcePos onFileOpen={p => setOpened(p)} />
      </BasePathCtx.Provider>
      <p data-testid="opened" className="mt-6 text-sm text-text-muted">Opened in panel: {opened}</p>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Harness />)
