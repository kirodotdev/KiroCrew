/**
 * Screenshot harness for the bedrock_kb add-source branch (issue #7947).
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server
 * with /api/** answered by fixtures. Captures the Sources tab's Add Source
 * dialog with the Bedrock KB type selected — empty (submit disabled) and
 * filled (submit enabled) in dark, plus the filled form in light.
 *
 * Doubles as a regression check: exits non-zero unless the type button, the
 * three fields, the live-query hint, and the submit gating are all present.
 *
 * Usage: node scripts/capture-bedrock-kb-source.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/bedrock-kb'
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const failures = []
const expect = (cond, msg) => { if (!cond) failures.push(msg) }

const knowledgeStubs = async (path, route) => {
  if (path === '/api/knowledge/sources') { await json(route, []); return true }
  if (path.startsWith('/api/aws/consent')) {
    await json(route, {
      service: 'bedrock-kb', serviceLabel: 'Amazon Bedrock (knowledge base retrieval)',
      profile: 'my-team-profile', credentialSource: 'named profile', region: 'us-east-1',
      account: '111122223333', arn: 'arn:aws:iam::111122223333:user/example',
      identityResolved: true, identityDetail: '', granted: false,
      reason: 'not granted', revokedOnAccountChange: false, grant: null,
    })
    return true
  }
  if (path === '/api/knowledge/config') {
    await json(route, { enabled: true, supported_formats: ['md', 'txt'], folder_picker: false })
    return true
  }
  if (path === '/api/knowledge/namespaces') { await json(route, []); return true }
  if (path === '/api/knowledge/stats') {
    await json(route, {
      items: 0, entities: 0, relations: 0, sources: 0,
      embeddings: { enabled: true, available: true, model: 'bge-small', embedded_items: 0 },
    })
    return true
  }
  return false
}

async function openBedrockForm(page) {
  await page.goto(`${base}/knowledge`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(800)
  const tab = page.getByRole('button', { name: /^sources$/i }).first()
  if (!(await tab.count())) throw new Error('Sources tab not found')
  await tab.click()
  await page.waitForTimeout(600)
  await page.getByRole('button', { name: /add source/i }).first().click()
  await page.waitForTimeout(400)
  const typeBtn = page.getByRole('button', { name: /bedrock knowledge base/i }).first()
  expect(await typeBtn.count(), 'Bedrock KB type button missing')
  await typeBtn.click()
  await page.waitForTimeout(400)
}

async function fillForm(page) {
  await page.getByLabel('Knowledge Base IDs').fill('KBEXAMPLE1')
  await page.getByLabel('AWS region', { exact: true }).fill('us-east-1')
  await page.getByLabel('AWS profile', { exact: true }).fill('my-team-profile')
  await page.waitForTimeout(300)
}

try {
  // Dark: empty (gated) + filled states.
  const dark = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  logPageProblems(dark)
  await stubDashboardApi(dark, { theme: 'dark', extra: knowledgeStubs })
  await openBedrockForm(dark)

  const submit = dark.getByRole('button', { name: /add knowledge base/i }).first()
  expect(await submit.count(), 'submit button missing')
  expect(await submit.isDisabled(), 'submit must be disabled while kb ids/region empty')
  const bodyText = await dark.locator('body').innerText()
  expect(/Queried live from your AWS account/i.test(bodyText), 'live-query hint missing')
  await dark.screenshot({ path: `${OUT}/bedrock-form-empty-dark.png` })

  await fillForm(dark)
  // Submit now gates on the grant as well: in this ask-state stub (granted:
  // false) the filled form must KEEP the button disabled — the card above is
  // the visible reason. The enabled state is asserted in the granted leg.
  expect(await submit.isDisabled(), 'submit must stay disabled while consent is ungranted')
  // The consent card mounts once a region exists: the filled shot must show
  // the account-bound ask (paid-service confirmation) with the probed account.
  await dark.waitForTimeout(400)
  const filledText = await dark.locator('body').innerText()
  expect(/111122223333/.test(filledText), 'consent card must show the probed account')
  expect(/Confirm the AWS account above/i.test(filledText), 'the disabled submit must carry its adjacent reason')
  await dark.screenshot({ path: `${OUT}/bedrock-form-filled-dark.png` })
  await dark.close()

  // Light: filled state.
  const light = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  logPageProblems(light)
  await stubDashboardApi(light, { theme: 'light', extra: knowledgeStubs })
  await openBedrockForm(light)
  await fillForm(light)
  await light.screenshot({ path: `${OUT}/bedrock-form-filled-light.png` })
  await light.close()

  // Connected-source row: the Live meta chip with sync/items/staleness hidden.
  const rowStubs = async (path, route) => {
    if (path === '/api/knowledge/sources') {
      await json(route, [{
        id: 'src-bedrock-1', source_type: 'bedrock_kb',
        uri: 'bedrock-kb://us-east-1/EXAMPLEKB01', name: 'Team design KB',
        item_count: 0, last_synced_at: null,
        properties: { kb_ids: 'EXAMPLEKB01', region: 'us-east-1', profile: 'my-team-profile' },
      }])
      return true
    }
    return knowledgeStubs(path, route)
  }
  const row = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  logPageProblems(row)
  await stubDashboardApi(row, { theme: 'dark', extra: rowStubs })
  await row.goto(`${base}/knowledge`, { waitUntil: 'networkidle' })
  await row.waitForTimeout(800)
  await row.getByRole('button', { name: /^sources$/i }).first().click()
  await row.waitForTimeout(600)
  const rowText = await row.locator('body').innerText()
  expect(/Live/.test(rowText), 'connected bedrock_kb row must show the Live chip')
  expect(!/Sync now/i.test(rowText), 'live source row must not offer Sync')
  await row.screenshot({ path: `${OUT}/bedrock-source-row-live-dark.png` })
  await row.close()

  // Consent GRANTED receipt inside the form (the post-confirmation state).
  const grantedStubs = async (path, route) => {
    if (path.startsWith('/api/aws/consent')) {
      await json(route, {
        service: 'bedrock-kb', serviceLabel: 'Amazon Bedrock (knowledge base retrieval)',
        profile: 'my-team-profile', credentialSource: 'named profile', region: 'us-east-1',
        account: '111122223333', arn: 'arn:aws:iam::111122223333:user/example',
        identityResolved: true, identityDetail: '', granted: true,
        reason: 'granted', revokedOnAccountChange: false,
        grant: {
          service: 'bedrock-kb', profile: 'my-team-profile', region: 'us-east-1',
          account: '111122223333', arn: 'arn:aws:iam::111122223333:user/example',
          granted_at: '2026-09-06T00:00:00+00:00',
        },
      })
      return true
    }
    return knowledgeStubs(path, route)
  }
  const granted = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  logPageProblems(granted)
  await stubDashboardApi(granted, { theme: 'dark', extra: grantedStubs })
  await openBedrockForm(granted)
  await fillForm(granted)
  await granted.waitForTimeout(400)
  const grantedText = await granted.locator('body').innerText()
  expect(/111122223333/.test(grantedText), 'granted receipt must name the confirmed account')
  const grantedSubmit = granted.getByRole('button', { name: /add knowledge base/i })
  expect(!(await grantedSubmit.isDisabled()), 'submit must enable once consent is granted')
  await granted.screenshot({ path: `${OUT}/bedrock-form-consent-granted-dark.png` })
  await granted.close()

} finally {
  await browser.close()
  srv.close()
}

if (failures.length) {
  console.error('FAILURES:')
  for (const f of failures) console.error(' -', f)
  process.exit(1)
}
console.log('OK — 5 screenshots in', OUT)
