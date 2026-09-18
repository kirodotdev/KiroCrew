/**
 * Isolated capture entry for the copy row under a rendered markdown table.
 *
 * WHY ISOLATED: a table only exists inside a rendered assistant turn, and
 * booting the full SPA for one needs the app shell, a live websocket and a
 * seeded session. The row is pure MarkdownRenderer output, so the renderer
 * alone -- with the real theme CSS and the real i18n catalog -- is faithful.
 *
 * The clipboard is the one seam stubbed: `navigator.clipboard.writeText` is
 * replaced with a recorder so the capture script can read back EXACTLY what a
 * click put on the clipboard and assert it, instead of trusting the tick.
 *
 * Scene + theme come from the query string: ?scene=wide&theme=light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does. Importing the module only DEFINES
// initI18n -- without calling it every label in the frame is blank, which
// silently produces screenshots that misrepresent the real UI.
import { initI18n } from '../src/i18n'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'default'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

declare global {
  interface Window { __copied: string[] }
}
window.__copied = []
Object.defineProperty(navigator, 'clipboard', {
  configurable: true,
  value: { writeText: async (text: string) => { window.__copied.push(text) } },
})

/** A typical agent answer: prose, a table with mixed alignment and inline
 *  formatting, prose. The right-aligned column is what the Markdown copy must
 *  round-trip and a DOM walk would lose. */
const DEFAULT = [
  'Here is the comparison you asked for:',
  '',
  '| Symbol | Price | MACD Hist | Signal |',
  '| --- | ---: | ---: | :---: |',
  '| `GOOGL` | $344.82 | -0.57 | **STRONG SELL** |',
  '| `AAPL` | $189.10 | 0.12 | HOLD |',
  '| `MSFT` | $412.55 | 1.04 | *BUY* |',
  '',
  'The signal column is derived from the histogram sign and magnitude.',
].join('\n')

/** Wider than the frame, so the table scrolls horizontally: the copy row must
 *  stay put while the table scrolls under it. */
const WIDE = [
  '| Region | Instance | vCPU | Memory | Network | Storage | On-demand $/h | Spot $/h | Availability zones | Notes |',
  '| --- | --- | ---: | ---: | --- | --- | ---: | ---: | --- | --- |',
  '| eu-west-1 | c7g.2xlarge | 8 | 16 GiB | up to 15 Gbps | EBS only | 0.2890 | 0.1120 | a, b, c | Graviton3 |',
  '| us-east-1 | c7g.2xlarge | 8 | 16 GiB | up to 15 Gbps | EBS only | 0.2900 | 0.1015 | a, b, c, d, f | Graviton3 |',
  '| ap-southeast-2 | c7g.2xlarge | 8 | 16 GiB | up to 15 Gbps | EBS only | 0.3480 | 0.1380 | a, b, c | Graviton3 |',
].join('\n')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
initI18n('en')

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <div data-capture-root className="bg-bg p-5 text-text" style={{ width: 640 }}>
        <MarkdownRenderer content={scene === 'wide' ? WIDE : DEFAULT} />
      </div>
    </QueryClientProvider>
  </MemoryRouter>,
)
