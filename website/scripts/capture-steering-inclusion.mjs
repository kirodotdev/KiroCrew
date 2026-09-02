/**
 * Capture the Steering tab's inclusion surfaces: the mode chips and the
 * editor's mode control.
 *
 * Gateway-free, like every other harness in this folder — it serves the real
 * built SPA and answers `/api/**` from `stub-dashboard-api.mjs`, plus the two
 * steering endpoints below. That matters beyond convenience here: the states
 * worth showing (a misspelled mode, an `auto` document) are ones a real home
 * would have to be hand-seeded with before every run.
 *
 *   npm run build && node scripts/capture-steering-inclusion.mjs <outDir>
 */
import { chromium } from '@playwright/test'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2]
if (!OUT) { console.error('usage: capture-steering-inclusion.mjs <outDir>'); process.exit(2) }

const doc = (rel, inclusion, declared, pattern, description) => ({
  key: `user/${rel}`, name: rel, rel, source: 'user',
  path: `~/.kiro/steering/${rel}`, size: 200, description,
  inclusion, inclusion_declared: declared, file_match_pattern: pattern,
})

// One document per state the tab distinguishes: no declaration, each honored
// mode, and a misspelling — which is the case the warning chip exists for.
const LIST = {
  files: [
    doc('api-design.md', 'auto', 'auto', '', 'API design'),
    doc('coding-conventions.md', 'always', '', '', 'Coding conventions'),
    doc('incident-runbook.md', 'manual', 'manual', '', 'Incident runbook'),
    doc('release-checklist.md', 'always', 'manaul', '', 'Release checklist'),
    doc('typescript-style.md', 'fileMatch', 'fileMatch', 'src/**/*.ts', 'TypeScript style'),
  ],
  roots: [{ source: 'user', path: '~/.kiro/steering', exists: true }],
  project: '', project_key: '', project_state: 'none',
}

const BODIES = {
  'user/api-design.md':
    '---\ninclusion: auto\nname: api-design\ndescription: REST API patterns.\n---\n'
    + '# API design\n\nResource naming, status codes and pagination conventions.\n',
  'user/typescript-style.md':
    '---\ninclusion: fileMatch\nfileMatchPattern: "src/**/*.ts"\n---\n'
    + '# TypeScript style\n\nNaming, import order and error handling.\n',
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
try {
  for (const scheme of ['dark', 'light']) {
    const ctx = await browser.newContext({
      viewport: { width: 1440, height: 900 }, colorScheme: scheme, locale: 'en-US',
    })
    const page = await ctx.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      theme: scheme,
      extra: async (path, route) => {
        if (path === '/api/steering') { await json(route, LIST); return true }
        if (path.startsWith('/api/steering/')) {
          const key = decodeURIComponent(path.slice('/api/steering/'.length))
          await json(route, {
            key, content: BODIES[key] ?? '# Doc\n\nbody\n',
            path: `~/.kiro/steering/${key}`, source: 'user',
          })
          return true
        }
        return false
      },
    })
    await page.goto(`${base}/capabilities?tab=steering`, { waitUntil: 'networkidle' })
    const list = page.getByRole('listbox', { name: 'Steering files' })
    await list.waitFor({ timeout: 20000 })
    await page.waitForTimeout(500)
    await list.screenshot({ path: `${OUT}/inclusion-chips-${scheme}.png` })

    await page.getByRole('button', { name: 'Select typescript-style.md' }).click()
    await page.waitForTimeout(600)
    await page.getByRole('button', { name: 'Edit', exact: true }).first().click()
    await page.waitForTimeout(500)
    await page.screenshot({ path: `${OUT}/mode-editor-${scheme}.png` })
    await ctx.close()
  }
} finally {
  await browser.close()
  srv.close()
}
console.log('ok')
