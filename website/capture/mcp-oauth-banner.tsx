/**
 * Evidence that the MCP OAuth banner's border and fill were not being emitted.
 *
 * THE BUG: the banner styled itself with `border-[var(--danger)]/40` and
 * `bg-[var(--danger)]/10`. An opacity modifier cannot be applied to an ARBITRARY
 * value, so Tailwind emitted no rule at all for those two classes -- the fill
 * disappeared and `border` alone fell back to Preflight's `#e5e7eb`, i.e. a
 * bright white line in every dark theme. The token form (`border-danger/40`)
 * compiles, because tailwind.config.js wraps each theme colour in `withAlpha`.
 *
 * WHY THIS FILE HAND-WRITES NO CLASSES. `tailwind.config.js` scans
 * `['./index.html', './src/**\/*.{ts,tsx}']` -- `capture/` is NOT in that glob.
 * A "before" state written as literal class strings HERE would render unstyled
 * no matter what those classes were, so it could not tell a genuinely un-emitted
 * rule apart from a file Tailwind never read. It would be unfalsifiable
 * evidence. So this harness only ever mounts the REAL component out of `src/`,
 * and the before/after difference comes from which version of that source is on
 * disk -- see the runner's header for the two-step procedure.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n'
import McpOAuthBanner from '../src/pages/chat/McpOAuthBanner'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const URL = 'https://example.com/login/oauth/authorize?client_id=abc123'

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <div
    data-capture-root
    className="bg-bg text-text flex flex-col gap-3 p-5"
    style={{ width: 620 }}
  >
    <div data-state="pending">
      <McpOAuthBanner serverName="github-mcp" oauthUrl={URL} completed={false} />
    </div>
    <div data-state="done">
      <McpOAuthBanner serverName="github-mcp" oauthUrl={URL} completed />
    </div>
    <div data-state="failed">
      <McpOAuthBanner serverName="github-mcp" oauthUrl={URL} completed={false} failed error="token exchange rejected" />
    </div>
  </div>,
)
