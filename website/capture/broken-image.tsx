/**
 * Isolated capture entry for the broken-image fallback chip.
 *
 * WHY ISOLATED: the chip only appears inside a rendered transcript image whose
 * bytes fail to load; booting the full SPA for that needs the app shell, a live
 * websocket and a seeded session. Here the failures are REAL, not mocked — the
 * missing local path 404s against the dev server's /api/file-raw and the remote
 * URL points at an unresolvable host, so `onError` fires exactly as it does in
 * production. The working image at the top loads from the dev server's own
 * public/ assets, pinning that the fallback replaces only broken images.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

// Initialise i18next as main.tsx does — `../src/i18n/all` registers every
// language catalog (plain `../src/i18n` is English-only) so the zh-CN scene
// renders translated labels instead of silently falling back to English.
import { initI18n } from '../src/i18n/all'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// The chip's HEAD probe re-asks /api/file-raw and shows the missing-file
// wording only on a confirmed 404. The dev server proxies /api to a backend
// that is not running here (502), so answer the probe with the same 404 the
// real endpoint returns for a nonexistent path — the stub replaces the
// backend, not the component.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/file-raw')) {
    return Promise.resolve(new Response(JSON.stringify({ error: 'not found' }), { status: 404 }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const MD = `一张正常加载的图片（对照）：

![正常图片](${location.origin}/icon-192.png)

本地截图文件已被临时目录清理：

![账户列表](/tmp/aws-control-tour/01-accounts.png)

同一情况、消息没写 alt 文本：

![](/tmp/aws-control-tour/02-console.png)

远程 URL 加载失败：

![部署拓扑图](https://img.invalid.example/chart.png)
`

async function main() {
  initI18n('zh-CN')
  const root = createRoot(document.getElementById('root')!)
  root.render(
    <div className="min-h-screen bg-bg text-text p-8" style={{ maxWidth: 720 }}>
      <MarkdownRenderer content={MD} />
    </div>,
  )
}

void main()
