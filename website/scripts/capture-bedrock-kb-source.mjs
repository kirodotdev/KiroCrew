/**
 * Screenshot harness for the bedrock_kb add-source branch (issue #7947).
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server
 * with /api/** answered by fixtures. Captures the Sources tab's Add Source
 * dialog with the Bedrock KB type selected — empty (submit disabled) and
 * filled (submit enabled) in dark, plus the filled form in light.
 *
 * Doubles as a regression check: exits non-zero unless the type button, the
 * three fields, the live-query hint, the submit gating, and the failed-remove
 * notice on a restored Live row are all present.
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
    // Echo the REQUESTED target, as the real GET does for bedrock-kb
    // (`_effective_target` returns the query's profile and region, and
    // `credentialSource` is derived from that profile). A fixed profile here
    // made the card name `my-team-profile` under a field reading
    // `my-team-profile-2` in the nudge shot -- a fixture artefact, not UI.
    const url = new URL(route.request().url())
    const profile = url.searchParams.get('profile') ?? ''
    const region = url.searchParams.get('region') ?? 'us-east-1'
    await json(route, {
      service: 'bedrock-kb', serviceLabel: 'Amazon Bedrock (knowledge base retrieval)',
      profile, region,
      credentialSource: profile ? `profile ${profile}` : 'AWS CLI default credential provider chain',
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
  const profile = page.getByLabel('AWS profile (optional)', { exact: true })
  await profile.fill('my-team-profile')
  // Commit the profile: the consent card names the blur-committed profile and
  // is withheld while the field has typed past it, so a filled form is one
  // whose profile has been left (Tab, or a click elsewhere), as a user's is.
  await profile.blur()
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
  expect(/Leave blank to use the default AWS credentials/i.test(bodyText), 'profile field must say what blank means')
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

  // Narrow (320px): the region/profile pair must STACK, not share a row.
  // Asserted geometrically: the profile input starts below the region
  // input's bottom edge, and neither is narrower than the form allows.
  const narrow = await browser.newPage({ viewport: { width: 320, height: 900 } })
  logPageProblems(narrow)
  await stubDashboardApi(narrow, { theme: 'dark', extra: knowledgeStubs })
  await openBedrockForm(narrow)
  await fillForm(narrow)
  const regionBox = await narrow.getByLabel('AWS region', { exact: true }).boundingBox()
  const profileBox = await narrow.getByLabel('AWS profile (optional)', { exact: true }).boundingBox()
  expect(regionBox && profileBox, 'region/profile inputs missing at 320px')
  expect(profileBox.y >= regionBox.y + regionBox.height, 'region and profile must stack at 320px, not sit side by side')
  expect(Math.abs(profileBox.width - regionBox.width) < 2, 'stacked fields must span the same width')
  expect(regionBox.width > 200, `region field compressed at 320px (${regionBox.width}px wide)`)
  await narrow.screenshot({ path: `${OUT}/bedrock-form-filled-narrow-dark.png`, fullPage: true })
  await narrow.close()

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
  expect(/Queried live from your AWS account at search time/i.test(rowText), 'live row must state its meaning as visible text')
  await row.screenshot({ path: `${OUT}/bedrock-source-row-live-dark.png` })
  // The remove confirm is the browser's native confirm() (same as the sibling
  // remove), so it cannot be screenshotted; assert the copy it carries.
  let removeDialog = ''
  row.once('dialog', async d => { removeDialog = d.message(); await d.dismiss() })
  await row.getByRole('button', { name: /remove source/i }).first().click()
  await row.waitForTimeout(300)
  expect(/Remove this knowledge base source\? Nothing is stored locally; the knowledge base itself is not touched\./.test(removeDialog),
    `live row remove must confirm with the no-local-data copy (got: ${JSON.stringify(removeDialog)})`)
  const afterDismiss = await row.locator('body').innerText()
  expect(/Team design KB/.test(afterDismiss), 'dismissing the remove confirm must keep the source')
  await row.close()

  // The remove confirmation for a Live source, as the user reads it before the
  // destructive click. Headless Chromium paints no native confirm(), so this
  // leg swaps `window.confirm` for an in-page stand-in that shows the exact
  // message the row passes it -- shipped copy, harness frame -- and answers
  // false (dismiss) once the shot is taken, so the row stays and no DELETE is
  // issued. The copy is asserted against the catalog string, as above.
  const removeConfirm = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  logPageProblems(removeConfirm)
  await stubDashboardApi(removeConfirm, { theme: 'dark', extra: rowStubs })
  await removeConfirm.goto(`${base}/knowledge`, { waitUntil: 'networkidle' })
  await removeConfirm.waitForTimeout(800)
  await removeConfirm.getByRole('button', { name: /^sources$/i }).first().click()
  await removeConfirm.waitForTimeout(600)
  await removeConfirm.evaluate(() => {
    window.confirm = (message) => {
      const box = document.createElement('div')
      box.setAttribute('role', 'alertdialog')
      box.setAttribute('data-testid', 'confirm-stand-in')
      box.style.cssText = 'position:fixed;top:120px;left:50%;transform:translateX(-50%);width:420px;'
        + 'padding:16px 20px;border:1px solid var(--border);border-radius:8px;background:var(--bg-elevated);'
        + 'color:var(--text);font:13px/1.5 system-ui,sans-serif;box-shadow:0 12px 32px rgba(0,0,0,.45);z-index:9999'
      const text = document.createElement('p')
      text.textContent = String(message)
      text.style.margin = '0 0 14px'
      const buttons = document.createElement('div')
      buttons.style.cssText = 'display:flex;justify-content:flex-end;gap:8px'
      for (const label of ['Cancel', 'OK']) {
        const b = document.createElement('button')
        b.textContent = label
        b.style.cssText = 'padding:6px 14px;border-radius:6px;border:1px solid var(--border);'
          + (label === 'OK' ? 'background:var(--accent);color:var(--accent-fg)' : 'background:var(--bg);color:var(--text)')
        buttons.appendChild(b)
      }
      box.append(text, buttons)
      document.body.appendChild(box)
      return false
    }
  })
  await removeConfirm.getByRole('button', { name: /remove source/i }).first().click()
  const standIn = removeConfirm.getByTestId('confirm-stand-in')
  await standIn.waitFor({ timeout: 5000 })
  expect(/Remove this knowledge base source\? Nothing is stored locally; the knowledge base itself is not touched\./.test(await standIn.innerText()),
    'the remove-confirm shot must carry the live-row copy')
  expect(/Team design KB/.test(await removeConfirm.locator('body').innerText()), 'the dismissed confirm must leave the source in place')
  await removeConfirm.screenshot({ path: `${OUT}/bedrock-source-remove-confirm-dark.png` })
  await removeConfirm.close()

  // Failed remove on the Live row: the confirm is ACCEPTED, the DELETE is
  // refused, the optimistically hidden row is restored, and the
  // `source-delete-error` ErrorNotice renders under it WITH the agent hand-off
  // (the Add form is closed, so nothing unsaved is at risk). The payload is the
  // handler's own attestation-revoke refusal, word for word, so the shot shows
  // shipped copy rather than a harness sentence (UX round asked for this state
  // as evidence).
  const removeFailStubs = async (path, route) => {
    if (path === '/api/knowledge/sources/src-bedrock-1' && route.request().method() === 'DELETE') {
      await route.fulfill({
        status: 500, contentType: 'application/json',
        body: JSON.stringify({
          error: "Couldn't remove this source. Nothing was deleted. Try again.",
          code: 'bedrock_kb_attestation_revoke_failed',
        }),
      })
      return true
    }
    return rowStubs(path, route)
  }
  const removeFail = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  logPageProblems(removeFail)
  await stubDashboardApi(removeFail, { theme: 'dark', extra: removeFailStubs })
  await removeFail.goto(`${base}/knowledge`, { waitUntil: 'networkidle' })
  await removeFail.waitForTimeout(800)
  await removeFail.getByRole('button', { name: /^sources$/i }).first().click()
  await removeFail.waitForTimeout(600)
  let acceptedDialog = ''
  removeFail.once('dialog', async d => { acceptedDialog = d.message(); await d.accept() })
  const deleteResponse = removeFail.waitForResponse(r =>
    r.request().method() === 'DELETE' && new URL(r.url()).pathname === '/api/knowledge/sources/src-bedrock-1')
  await removeFail.getByRole('button', { name: /remove source/i }).first().click()
  expect((await deleteResponse).status() === 500, 'accepting the remove confirm must issue the DELETE the stub refuses')
  expect(/Remove this knowledge base source\?/.test(acceptedDialog),
    `failed-remove leg must go through the live-row confirm (got: ${JSON.stringify(acceptedDialog)})`)
  await removeFail.waitForTimeout(600)
  const removeFailText = await removeFail.locator('body').innerText()
  expect(/Team design KB/.test(removeFailText), 'a refused delete must restore the source row')
  const deleteNotice = removeFail.getByTestId('source-delete-error')
  const deleteNoticeCount = await deleteNotice.count()
  expect(deleteNoticeCount === 1, 'a refused delete must render the source-delete-error notice under the restored row')
  // Read the notice only when it exists: a locator read on a missing element
  // auto-waits 30s and dies as a stack trace instead of a FAILURES line.
  if (deleteNoticeCount === 1) {
    expect(/Couldn't remove this source\. Nothing was deleted\. Try again\./.test(await deleteNotice.innerText()),
      'the delete notice must carry the server refusal copy')
    expect(await deleteNotice.getByRole('button', { name: /ask the agent/i }).count(), 'the delete notice must offer the agent hand-off while the add form is closed')
    const noticeBox = await deleteNotice.boundingBox()
    expect(noticeBox && noticeBox.y + noticeBox.height <= 900, 'the delete notice must sit inside the 1440x900 viewport shot')
  }
  await removeFail.screenshot({ path: `${OUT}/bedrock-source-row-remove-failed-dark.png` })
  await removeFail.close()

  // Consent GRANTED receipt inside the form (the post-confirmation state).
  const grantedStubs = async (path, route) => {
    if (path.startsWith('/api/aws/consent')) {
      await json(route, {
        service: 'bedrock-kb', serviceLabel: 'Amazon Bedrock (knowledge base retrieval)',
        profile: 'my-team-profile', credentialSource: 'profile my-team-profile', region: 'us-east-1',
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

  // Region missing: KB ids typed, region still empty. The third way a filled
  // form can sit with a dead Add, and the one that had no adjacent reason
  // (UX round asked for this state as evidence).
  const missing = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  logPageProblems(missing)
  await stubDashboardApi(missing, { theme: 'dark', extra: knowledgeStubs })
  await openBedrockForm(missing)
  await missing.getByLabel('Knowledge Base IDs').fill('KBEXAMPLE1')
  await missing.waitForTimeout(300)
  const missingSubmit = missing.getByRole('button', { name: /add knowledge base/i }).first()
  expect(await missingSubmit.isDisabled(), 'submit must be disabled while the region is empty')
  const missingText = await missing.locator('body').innerText()
  expect(/Enter the AWS region to add this source/i.test(missingText), 'empty region must show its adjacent reason')
  await missing.screenshot({ path: `${OUT}/bedrock-form-region-missing-dark.png` })
  await missing.close()

  // Folder branch: the same visible-label style as the Bedrock branch, so
  // switching type does not flip the dialog's labeling (UX round).
  const folder = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  logPageProblems(folder)
  await stubDashboardApi(folder, { theme: 'dark', extra: knowledgeStubs })
  await folder.goto(`${base}/knowledge`, { waitUntil: 'networkidle' })
  await folder.waitForTimeout(800)
  await folder.getByRole('button', { name: /^sources$/i }).first().click()
  await folder.waitForTimeout(400)
  await folder.getByRole('button', { name: /add source/i }).first().click()
  await folder.getByRole('button', { name: /^local folder$/i }).first().click()
  await folder.waitForTimeout(300)
  for (const name of ['Source name (optional)', 'Folder path', 'Ignore patterns']) {
    const id = await folder.getByLabel(name, { exact: true }).first().getAttribute('id')
    expect(id && (await folder.locator(`label[for="${id}"]`).count()) === 1, `folder branch: ${name} must carry a visible label`)
  }
  await folder.screenshot({ path: `${OUT}/folder-form-labelled-dark.png` })
  await folder.close()

  // Region-format warning: a malformed region ("useast-1") must surface its
  // inline reason (UX round asked for this state as evidence).
  const warn = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  logPageProblems(warn)
  await stubDashboardApi(warn, { theme: 'dark', extra: knowledgeStubs })
  await openBedrockForm(warn)
  await warn.getByLabel('Knowledge Base IDs').fill('KBEXAMPLE1')
  await warn.getByLabel('AWS region', { exact: true }).fill('useast-1')
  await warn.waitForTimeout(300)
  const warnText = await warn.locator('body').innerText()
  expect(/Region must look like/i.test(warnText), 'malformed region must show the format hint')
  await warn.screenshot({ path: `${OUT}/bedrock-form-region-warning-dark.png` })
  await warn.close()

  // Add-failure ErrorNotice: the server refuses the add and the inline error
  // renders (UX round asked for this state as evidence). The payload is the
  // handler's own conflict refusal, word for word, so the shot shows shipped
  // copy rather than a harness sentence.
  const failStubs = async (path, route) => {
    if (path === '/api/knowledge/sources' && route.request().method() === 'POST') {
      await route.fulfill({
        status: 400, contentType: 'application/json',
        body: JSON.stringify({
          code: 'bedrock_kb_target_conflict',
          error: 'Team design KB (default profile, us-west-2) already uses a different AWS profile or region. '
            + 'Only one is supported for Bedrock Knowledge Base sources right now; remove that source first.',
        }),
      })
      return true
    }
    return grantedStubs(path, route)
  }
  const fail = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  logPageProblems(fail)
  await stubDashboardApi(fail, { theme: 'dark', extra: failStubs })
  await openBedrockForm(fail)
  await fillForm(fail)
  // Commit the profile (a real click blurs it first); the grant stub grants
  // any (profile, region), so the button is ready and the click POSTs.
  await fail.getByLabel('AWS profile (optional)', { exact: true }).blur()
  await fail.waitForTimeout(400)
  const failSubmit = fail.getByRole('button', { name: /add knowledge base/i }).first()
  await failSubmit.click()
  await fail.waitForTimeout(500)
  const failText = await fail.locator('body').innerText()
  expect(/already uses a different AWS profile or region/i.test(failText), 'add failure must render the server refusal copy inline')
  await fail.screenshot({ path: `${OUT}/bedrock-form-add-error-dark.png` })
  await fail.close()

  // Blocked-submit nudge: the grant covers the blur-committed profile, the
  // user then edits the profile WITHOUT blurring and clicks Add with a real
  // pointer. The pointerdown commits the typed profile before the blur could
  // disable the button under the click, re-targets the card (now ungranted),
  // rings it and turns the adjacent reason warn-toned. The stub grants only
  // the committed profile so the re-targeted card lands in the ask state.
  const nudgeStubs = async (path, route) => {
    if (path.startsWith('/api/aws/consent')) {
      const url = new URL(route.request().url())
      if (url.searchParams.get('profile') === 'my-team-profile') return grantedStubs(path, route)
      return knowledgeStubs(path, route)
    }
    return knowledgeStubs(path, route)
  }
  const nudge = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  logPageProblems(nudge)
  await stubDashboardApi(nudge, { theme: 'dark', extra: nudgeStubs })
  await openBedrockForm(nudge)
  await fillForm(nudge)
  const profileInput = nudge.getByLabel('AWS profile (optional)', { exact: true })
  await profileInput.blur()
  await nudge.waitForTimeout(500)
  const nudgeSubmit = nudge.getByRole('button', { name: /add knowledge base/i }).first()
  expect(!(await nudgeSubmit.isDisabled()), 'nudge leg: submit must be enabled for the granted committed profile')
  await profileInput.focus()
  await profileInput.pressSequentially('-2')
  await nudgeSubmit.click()
  await nudge.waitForTimeout(600)
  const card = nudge.getByTestId('bedrock-kb-consent-card')
  expect(/ring-2/.test((await card.getAttribute('class')) || ''), 'blocked submit must ring the consent card')
  const reason = nudge.getByRole('status').filter({ hasText: /Confirm the AWS account above/i })
  expect(await reason.count(), 'blocked submit must show the adjacent reason')
  expect(/text-warn/.test((await reason.first().getAttribute('class')) || ''), 'blocked submit must turn the reason warn-toned')
  await nudge.screenshot({ path: `${OUT}/bedrock-form-consent-nudge-dark.png` })
  await nudge.close()

} finally {
  await browser.close()
  srv.close()
}

if (failures.length) {
  console.error('FAILURES:')
  for (const f of failures) console.error(' -', f)
  process.exit(1)
}
console.log('OK — 13 screenshots in', OUT)
