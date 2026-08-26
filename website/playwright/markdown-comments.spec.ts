import { test, expect } from '@playwright/test'
import * as os from 'os'

test.describe('Markdown Panel with Comments', () => {
  test('file-watch rejects sensitive paths', async ({ page }) => {
    const res = await page.request.get('/api/file-watch?path=' + encodeURIComponent(os.homedir() + '/.aws/credentials'))
    expect(res.status()).toBe(400)
  })

  test('file-watch rejects empty path', async ({ page }) => {
    const res = await page.request.get('/api/file-watch?path=')
    expect(res.status()).toBe(400)
  })
})
