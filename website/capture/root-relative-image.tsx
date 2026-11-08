/**
 * Isolated capture entry for root-relative images in a previewed markdown file
 * (issue #23623).
 *
 * The real MarkdownRenderer renders inside the same BasePathCtx the file
 * preview (ContentRenderer) provides, for a file at `?file=`. The <img> loads
 * hit /api/file-raw, which the shoot script answers from a real directory on
 * disk: a file that exists is served, any other path 404s, the same contract as
 * the backend endpoint. So which candidate loads is decided by the filesystem,
 * not by the entry.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import MarkdownRenderer, { BasePathCtx } from '../src/components/MarkdownRenderer'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const file = params.get('file') || ''

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// The broken-image chip's HEAD probe goes through fetch, which page.route does
// not see for a same-origin dev-server request in every Chromium build; answer
// it the way the endpoint would for a path that does not exist.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/file-raw') && init?.method === 'HEAD') {
    return Promise.resolve(new Response(null, { status: 404 }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const MD = `## Site-root image

Written the way GitHub, Zenn and Docusaurus expect, from the root of the repository:

![Architecture diagram](/images/post/architecture.png)

## Image that is really missing

![Screenshot that was never committed](/images/post/missing.png)
`

async function main() {
  initI18n('en')
  const root = createRoot(document.getElementById('root')!)
  root.render(
    <div className="min-h-screen bg-bg text-text p-8" style={{ maxWidth: 720 }}>
      <BasePathCtx.Provider value={file}>
        <MarkdownRenderer content={MD} />
      </BasePathCtx.Provider>
    </div>,
  )
}

void main()
