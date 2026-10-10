import { test, expect } from '@playwright/test'

/**
 * The link `dashboard_preview` hands a crewmate resolves, end to end.
 *
 * The flow is "stage a page, show the person, ask, then apply". A link the person
 * cannot open means they are asked to approve a page nobody could see -- which is
 * what happened while the link named only the slug (refused `missing_member`) and
 * pointed at the JSON API (raw JSON in the tab). This spec is the guard against that.
 *
 * The harness (`test_playwright_e2e._stage_dashboard_preview`) seeds one crewmate on
 * this ephemeral gateway, stages a catalog page for it with the PRODUCTION store, and
 * passes the link that store's `wire()` returns -- the exact value the tool reply
 * carries. Nothing here builds the link.
 */
interface StagedPreview {
  member: string
  url: string
}

function stagedPreview(): StagedPreview {
  expect(process.env.KIROCREW_E2E_EPHEMERAL, 'Use the isolated gateway E2E harness').toBe('1')
  const raw = process.env.KIROCREW_E2E_DASHBOARD_PREVIEW
  expect(raw, 'the harness stages a dashboard preview and passes its link').toBeTruthy()
  return JSON.parse(raw as string) as StagedPreview
}

test('the link dashboard_preview returns opens the staged page in the Dashboard tab', async ({ page, request, baseURL }) => {
  const staged = stagedPreview()

  // The UI route renders it: opening the link as given lands on the Dashboard tab,
  // under its preview band, with the STAGED template in the frame. `approvals_pending`
  // is a standup field the default page (project-report) does not have, so a tab
  // showing the live page cannot pass this -- nor can a link a browser shows as JSON.
  await page.goto(staged.url)
  const band = page.getByTestId('crew-dashboard-preview-band')
  await expect(band).toBeVisible({ timeout: 15000 })
  await expect(band).toContainText(`${staged.member} made this draft for you to look at.`)
  await expect(page.getByTestId('crew-dashboard-iframe')).toBeVisible({ timeout: 15000 })
  const frame = page.frameLocator('[data-testid="crew-dashboard-iframe"]')
  await expect(frame.locator('[data-dashboard-field="approvals_pending"]').first()).toBeAttached({ timeout: 15000 })

  // And the read behind it is accepted for the crewmate the link names: the link
  // carries the exact name (a slug alone is refused `missing_member`), and the
  // route answers the staged page, composed.
  const member = new URL(staged.url, baseURL).searchParams.get('member')
  expect(member, 'the link names the crewmate').toBe(staged.member)
  const roster = await request.get('/api/members')
  expect(roster.ok(), await roster.text()).toBeTruthy()
  const row = ((await roster.json()).members as { name: string; slug: string }[])
    .find(m => m.name === member)
  expect(row, `${member} is on the roster`).toBeTruthy()
  const read = await request.get(`/api/members/${encodeURIComponent(row!.slug)}/dashboard`, {
    params: { member: member!, preview: '1' },
  })
  expect(read.status(), await read.text()).toBe(200)
  const body = await read.json()
  expect(body.preview).toBe(true)
  expect(body.template.id).toBe('standup')
  expect(body.rendered_html).toContain('data-dashboard-field="approvals_pending"')
})
