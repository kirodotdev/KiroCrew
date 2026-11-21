import { createRoot } from 'react-dom/client'
import { useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { initI18n } from '../src/i18n/lazy'
import { store } from '../src/store'
import ChatInput from '../src/components/ChatInput'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import type { PasteBlock } from '../src/utils/pasteTokens'
import 'katex/dist/katex.min.css'
import '../src/index.css'

const params = new URLSearchParams(location.search)
document.documentElement.dataset.theme = params.get('theme') === 'light' ? 'kiro-light' : 'kiro-dark'
initI18n('en')

const content = [
  '## חדשות AWS בישראל',
  '',
  'ההרצה הבאה תתחיל בשעה 09:00 (שעון ישראל). קישור: [AWS](https://aws.amazon.com/).',
  '',
  'English paragraph stays left to right (123).',
  '',
  '- השירות זמין עכשיו בישראל (Tel Aviv).',
  '- פתחו את הקובץ `aws-news-seen.json` ובדקו את התוצאה.',
  '',
  '> שָׁלוֹם — ההרצה הסתיימה בהצלחה בשעה 09:00.',
  '',
  '- [ ] בדקו את ההודעה החדשה.',
  '- [x] שמרו את ההכרזות שכבר הוצגו.',
  '',
  '| אזור | Region |',
  '| --- | --- |',
  '| ישראל | il-central-1 |',
  '',
  '```sh',
  'echo "שלום"',
  'aws s3 ls s3://example-bucket',
  '```',
  '',
  '[קישור בעברית](https://example.com/)',
  '',
  '> ## כותרת בתוך ציטוט',
  '',
  '> - פריט בתוך ציטוט',
  '',
  'משוואה \\(x - y = 12\\) בעברית.',
  '',
  '<p dir="auto"><a href="https://example.com/">שלום מפורש</a></p>',
  '',
  '<div dir="auto"><p><a href="https://example.com/">קישור מפורש בתיבה</a></p></div>',
].join('\n')

function Scene() {
  const [draft, setDraft] = useState('שלום AWS (123).\nEnglish paragraph.\n123 — עברית')
  const [submitted, setSubmitted] = useState('')
  const [blocks, setBlocks] = useState<PasteBlock[]>([])
  return (
    <main className="bg-bg text-text min-h-screen p-5 mx-auto max-w-3xl">
      <MarkdownRenderer content={content} streaming={params.has('stream')} glow={params.has('stream')} />
      <section className="mt-5" data-testid="rtl-composer">
        <ChatInput value={draft} onChange={setDraft} onSend={() => setSubmitted(draft)} pasteBlocks={blocks} onPasteBlocksChange={setBlocks} lexicalComposer={params.has('lexical')} connected />
      </section>
      <output data-testid="submitted" className="sr-only">{submitted}</output>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <Provider store={store}><MemoryRouter><Scene /></MemoryRouter></Provider>
  </QueryClientProvider>,
)
