import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

// Browsers fetch the web app manifest without cookies unless the link opts in.
// Behind a cookie-gated tunnel that bare request gets a 403 HTML page, so the
// browser finds no manifest and never offers "Install as app".
const html = () => readFile(join(__dirname, '..', '..', 'index.html'), 'utf8')

describe('PWA manifest link', () => {
  it('sends credentials with the manifest request', async () => {
    const link = (await html()).match(/<link[^>]*rel="manifest"[^>]*>/)
    expect(link, 'expected a manifest link').not.toBeNull()
    expect(link![0]).toContain('crossorigin="use-credentials"')
  })
})
