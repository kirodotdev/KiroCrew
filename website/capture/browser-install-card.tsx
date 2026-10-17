/**
 * Isolated capture entry for the in-chat browser-support install card.
 *
 * The card is the real component, rendered under a reply that carries the real
 * marker; only the gateway answers are fixtures, chosen by `?scene=`:
 * needed | running | installed | installed-continued | failed | refused |
 * unavailable. `?theme=light` switches the theme.
 * Run `node capture/shoot-browser-install-card.mjs <outdir>` from website/.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { ApiError } from '../src/api/apiError'
import { api, type BrowserInstallData, type BrowserInstallJob } from '../src/api/client'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import { initI18n } from '../src/i18n/all'
import BrowserInstallCard from '../src/pages/chat/BrowserInstallCard'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const scene = params.get('scene') ?? 'needed'
document.documentElement.setAttribute('data-theme', theme)

const base: BrowserInstallData = {
  installed: false, cli_path: null, cli_version: null, node_ok: true, node_version: '22.11.0',
  browser_ok: false, installing: false, last_error: null, token: false, install_job: null,
}
const job = (over: Partial<BrowserInstallJob>): BrowserInstallJob => ({
  id: 'c1', kind: 'cli_setup', engine: null, status: 'running', stage: 'downloading_browser',
  started_at: '2026-10-17T15:00:00Z', updated_at: '2026-10-17T15:00:42Z', finished_at: null,
  elapsed_s: 42, error_code: null, error_detail: null, ...over,
})
const answers: Record<string, BrowserInstallData> = {
  needed: base,
  running: { ...base, installing: true, install_job: job({}) },
  installed: { ...base, installed: true, browser_ok: true, install_job: job({ status: 'succeeded', stage: 'finishing' }) },
  failed: { ...base, install_job: job({ status: 'failed', error_code: 'step_failed', error_detail: 'npm not found; install Node.js 20 or newer first' }) },
}
// `installed-continued` is the installed answer; the shooter clicks Continue.
answers['installed-continued'] = answers.installed
const answer = answers[scene] ?? base
// The gateway's own owner-gate refusal (403 owner_only), for `refused`.
const ownerOnly = () =>
  new ApiError(403, 'owner authorization required', '{"error":"owner authorization required","code":"owner_only"}')
Object.assign(api, {
  getBrowserInstall: async () => {
    // `unavailable`: the status read itself fails.
    if (scene === 'unavailable') throw new ApiError(502, 'Bad gateway', '')
    return answer
  },
  installBrowserCli: async () => {
    if (scene === 'refused') throw ownerOnly()
    return answer
  },
})

const REPLY = [
  "I couldn't open that page yet: browser support isn't installed on this computer.",
  'It needs a one-time install. Use the card below and I can pick up where we left off.',
  '',
  '<!-- kirocrew:browser-install -->',
].join('\n')

initI18n()
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <div className="bg-bg text-text min-h-screen p-8">
      <div className="max-w-[720px] text-[14px]" data-testid="capture-reply">
        <MarkdownRenderer content={REPLY} />
        <BrowserInstallCard content={REPLY} />
      </div>
    </div>
  </QueryClientProvider>,
)
