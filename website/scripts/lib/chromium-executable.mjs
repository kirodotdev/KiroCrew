/**
 * Shared Chromium resolution for the capture harnesses: honour
 * PLAYWRIGHT_CHROMIUM, else the newest cached headless shell, else return
 * undefined so `chromium.launch()` falls back to the Playwright pin.
 *
 * Why this exists: `website/node_modules/playwright` pins one browser
 * revision, but this machine's `~/.cache/ms-playwright` may only hold builds
 * fetched by a DIFFERENT playwright (the globally installed `@playwright/cli`,
 * say) — in which case a bare `chromium.launch()` dies with "Executable
 * doesn't exist at …chromium_headless_shell-<pinned>". Falling back to the
 * newest cached shell keeps the harnesses runnable on such machines.
 *
 * Extracted from capture-diff-split-preference.mjs so sibling harnesses can
 * share it instead of cloning it (jscpd runs at a 0% duplication threshold).
 */
import { existsSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'

export function chromiumExecutable() {
  if (process.env.PLAYWRIGHT_CHROMIUM) return process.env.PLAYWRIGHT_CHROMIUM
  const cache = join(homedir(), '.cache', 'ms-playwright')
  if (!existsSync(cache)) return undefined
  const rev = d => parseInt((/-(\d+)$/.exec(d) || [])[1] || '0', 10)
  const candidates = readdirSync(cache)
    .filter(d => d.startsWith('chromium_headless_shell-') || d.startsWith('chromium-'))
    .sort((a, b) => rev(b) - rev(a))
    .map(d => [
      join(cache, d, 'chrome-headless-shell-mac-arm64', 'chrome-headless-shell'),
      join(cache, d, 'chrome-headless-shell-linux64', 'chrome-headless-shell'),
      join(cache, d, 'chrome-linux64', 'chrome'),
      join(cache, d, 'chrome-linux', 'chrome'),
    ])
    .flat()
  return candidates.find(existsSync)
}
