import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')

describe('Webhooks first-source form', () => {
  it('keeps the source label input full-width in the focused empty state', () => {
    const source = fs.readFileSync(path.join(ROOT, 'src/pages/WebhooksPage.tsx'), 'utf8')
    expect(source).toMatch(/<Input\s+className="w-full"\s+placeholder=\{i18nT\('pages\.webhooksPage\.label_e_g_review_bot'\)\}/)
    expect(source).toContain('max-w-2xl flex-col py-8')
  })
})
