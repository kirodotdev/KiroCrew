/**
 * Shared /api/** fixture stub for the screenshot harnesses in this folder.
 *
 * Every harness runs the REAL built SPA gateway-free, which means every harness
 * needs the same ~25 endpoint stubs just to get the dashboard to boot: the
 * prerequisite gate, theme/branding, auth, status, notifications. Only the
 * folders and slots differ per harness, so keeping the boot fixtures here means
 * one copy instead of one per capture script (jscpd flags the duplication, and
 * more importantly a new endpoint added to the boot path had to be patched into
 * every script by hand).
 *
 * `extra` lets a harness override or add a route without forking the whole map:
 * it is consulted FIRST, and returning a truthy value from a handler marks the
 * request handled.
 */

/** Fulfil a Playwright route with a JSON body. */
export const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

/**
 * The `/api/config/kirocrew` body, matching `KiroCrewCfg` in
 * `website/src/pages/overview/KiroCrewCfgTab.tsx`.
 *
 * Named ahead of the catch-all because that tab does
 * `Object.entries(cfg.agents)` on mount. Under the catch-all's `{}`,
 * `cfg.agents` is `undefined`, `Object.entries(undefined)` throws, the app-shell
 * error boundary catches it, and the WHOLE PAGE renders blank — while the
 * harness still exits 0 and still writes a PNG. That failure mode fails toward
 * a false pass: a PR can cite a screenshot of an error boundary as evidence.
 *
 * Exported so a harness that needs a variation can spread it rather than
 * hand-rolling the shape again. Eighteen harnesses had already done exactly
 * that, and they had drifted — several spelled a workspace's directory `path`
 * where the component reads `dir`, so those rows rendered blank in the
 * screenshots that were supposed to prove them.
 */
export const KIROCREW_CONFIG_FIXTURE = {
  agents: {
    kirocrew: { kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' },
  },
  default_agent: 'kirocrew',
  workspaces: { default: { dir: '~/.kiro/crew/workspace' } },
  default_workspace: 'default',
  memory_stores: {
    default: { description: 'Default store', embedding_provider: '' },
  },
  default_memory_store: 'default',
  agent: {
    default_agent: 'kirocrew', provider: 'acp', model: 'auto',
    approval_mode: 'interactive', sandbox: 'auto',
    subagent_max_turns: 100, max_subagents: 3, subagent_auto_max: 16,
    conductor_skill: false, tool_search: true,
    max_channels: 8, max_channel_agents: 4, enforce_denied_commands: 'all',
  },
  session: { timeout_secs: 1800, pool_size: 2, pool_agent: 'kirocrew', pool_ttl_secs: 600 },
  memory: { embedding_provider: 'local' },
  auto_update: true,
}

/** The `/api/agent/config` body — the per-agent MCP view. */
export const AGENT_CONFIG_FIXTURE = { name: 'kirocrew', mcpServers: {} }

/** Whether an unmapped path should be guessed as an object rather than a list. */
const objectish = path =>
  /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)

/**
 * Install the gateway-free API stub on a page.
 *
 * @param {import('playwright').Page} page
 * @param {{
 *   folders?: unknown[],
 *   slots?: unknown[],
 *   theme?: string,
 *   botName?: string,
 *   preserveStorage?: boolean,
 *   localStorageEntries?: Record<string, string>,
 *   extra?: (path: string, route: import('playwright').Route) => unknown,
 * }} opts
 */
export async function stubDashboardApi(page, opts = {}) {
  const {
    folders = [],
    slots = [],
    theme = 'dark',
    preserveStorage = false,
    // Extra localStorage seeds applied INSIDE this stub's own init script,
    // after its clear. Playwright does not define the evaluation order of
    // separately registered init scripts, so a harness that seeds storage via
    // its own addInitScript races the clear below — pass the entries here
    // instead.
    localStorageEntries = null,
    // The backend's own default (`api_branding`: `cfg.dashboard.bot_name or
    // "Kiro Crew"`). It must stay TWO WORDS: the nav brand row accents the last
    // word only, and the composer placeholder interpolates the whole name — so
    // a single-word "Kiro" here silently produced screenshots with no "CREW"
    // and a "Message Kiro…" composer, in every harness in this folder.
    botName = 'Kiro Crew',
    extra = null,
  } = opts

  // Per-install, so a long harness reports each unmapped path once rather than
  // once per poll of the same endpoint.
  const announced = new Set()

  // Swallow the dashboard's websocket so it does not retry-storm with no gateway.
  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname

    if (extra && (await extra(path, route))) return

    if (path === '/api/kiro-prerequisite') {
      return json(route, {
        platform: 'darwin', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: false,
        repair_required: false, docs_url: '', setup_allowed: false,
        operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
      })
    }
    if (path === '/api/chat/folders') return json(route, folders)
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/status') {
      return json(route, { sessions: slots.length, crons: 0, lessons: 0, uptime: 120, version: '0.5.0' })
    }
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: theme, theme: '' })
    // Mirrors `api_branding` exactly, avatar included. Returning '' let the
    // frontend fall back to its own '/logo.png' default, which is the same URL —
    // but being explicit keeps the fixture readable as "what the server sends".
    if (path === '/api/dashboard/branding') return json(route, { bot_name: botName, avatar: '/logo.png' })
    if (path === '/api/recent-projects') return json(route, { dirs: [] })
    if (path === '/api/dashboard/config') {
      return json(route, {
        restore_sessions: false, restore_window_minutes: 30,
        merge_queued_messages: false, widget_density: 'more',
      })
    }
    if (path === '/api/agents' || path === '/api/chat/agents') {
      return json(route, [{ name: 'kirocrew', source: 'builtin' }, { name: 'oncall', source: 'aim' }])
    }
    // Named BEFORE the catch-all: both paths match its `config` test, and the
    // `{}` it would return blanks the whole Developer > Config surface (see
    // KIROCREW_CONFIG_FIXTURE for why).
    if (path === '/api/config/kirocrew') return json(route, KIROCREW_CONFIG_FIXTURE)
    if (path === '/api/agent/config') return json(route, AGENT_CONFIG_FIXTURE)
    // Endpoints not worth naming individually: anything object-shaped gets {},
    // everything else gets []. Guessing wrong only costs an empty panel — in
    // the cases where it does not, the endpoint belongs above this line.
    //
    // Announced once per path per run. The fixture gap this catch-all produced
    // for /api/config/kirocrew was silent AND fatal, and a harness author with
    // no reason to suspect the stub had nothing to go on: the page just came
    // back blank. A guess is a reasonable default, but it should say so, so the
    // NEXT unmapped endpoint is discoverable rather than mysterious.
    if (!announced.has(path)) {
      announced.add(path)
      console.log(`STUB: no fixture for ${path} — guessing ${objectish(path) ? '{}' : '[]'}`)
    }
    return json(route, objectish(path) ? {} : [])
  })

  await page.addInitScript(([themeMode, keepStorage, entries]) => {
    if (!keepStorage) localStorage.clear()
    localStorage.setItem('mc-theme', themeMode)
    localStorage.setItem('mc-onboarded', '1')
    for (const [k, v] of Object.entries(entries || {})) localStorage.setItem(k, v)
  }, [theme, preserveStorage, localStorageEntries])
}

/** Surface page errors + console errors on stdout so a broken capture is obvious. */
export function logPageProblems(page) {
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })
}
