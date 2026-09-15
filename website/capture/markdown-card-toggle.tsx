/**
 * Isolated capture entry for the #9196 markdown content card Formatted/Raw
 * toggle (capture/markdown-card-toggle.html).
 *
 * WHY ISOLATED: the card only appears inside a rendered chat transcript, which
 * needs the app shell, a live websocket and a seeded session to boot. Here the
 * exact production surface is reproduced by rendering MarkdownRenderer with
 * `mdCardToggle` — the same prop the chat transcript passes (AssistantMessage)
 * and no other surface does — over a ```markdown fence. The toggle, its
 * default, and both rendered/raw views are all real, not mocked.
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

// A ```markdown fence carrying the kind of structure the issue is about: a
// heading, a list, a table and a link — the parts that are materially faster to
// read rendered than as raw source.
const MD = [
  '```markdown',
  '# Release notes',
  '',
  'A short **overview** paragraph with a [link](https://example.com).',
  '',
  '- first item',
  '- second item',
  '',
  '| Surface | Has toggle |',
  '| --- | --- |',
  '| Tool detail card | yes |',
  '| Markdown content card | now yes |',
  '```',
].join('\n')

async function main() {
  initI18n('en')
  const root = createRoot(document.getElementById('root')!)
  root.render(
    <div className="min-h-screen bg-bg text-text p-8" style={{ maxWidth: 760 }}>
      <MarkdownRenderer content={MD} mdCardToggle />
    </div>,
  )
}

void main()
