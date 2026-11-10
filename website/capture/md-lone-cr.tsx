/**
 * Isolated capture entry for a Markdown file whose fenced code block uses
 * lone carriage-return line breaks, including the one before the closing fence.
 *
 * Renders through the real MarkdownRenderer with demo content only.
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n/all'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const MD = '# Project layout\n\nThe demo project is laid out like this:\n\n```text\r'
  + 'acme-demo/\r├── docs/\r│   └── index.md\r└── src/\r    └── main.ts\r```\n\nThat is the whole tree.\n'

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <div className="min-h-screen bg-bg text-text p-8" style={{ maxWidth: 720 }}>
    <MarkdownRenderer content={MD} />
  </div>,
)
