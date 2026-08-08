/**
 * Screenshot and browser-QA harness for deleting meetings from the list.
 *
 * Runs the real production SPA with deterministic API fixtures, following the
 * repository's other capture harnesses. It exercises available and protected
 * delete controls, the confirmation, and the successful list refresh.
 *
 * Usage: node scripts/capture-meetings-delete.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/meetings-delete'
mkdirSync(OUT, { recursive: true })

const ORIGINAL_MEETINGS = [
  {
    event_id: 'weekly-product-sync',
    title: 'Weekly product sync',
    status: 'ended',
    started_at: '2026-08-08T15:00:00Z',
    ended_at: '2026-08-08T16:00:00Z',
  },
  {
    event_id: 'customer-interview',
    title: 'Live customer interview',
    status: 'active',
    started_at: '2026-08-08T13:00:00Z',
    ended_at: '',
  },
  {
    event_id: 'architecture-review',
    title: 'Architecture review',
    status: 'paused',
    started_at: '2026-08-08T11:00:00Z',
    ended_at: '',
  },
  {
    event_id: 'launch-retro',
    title: 'Launch retrospective',
    status: 'reviewing',
    started_at: '2026-08-08T09:00:00Z',
    ended_at: '',
  },
]

const CALENDAR_EVENTS = [{
  event_id: 'planning-session',
  title: 'Tomorrow’s planning session',
  start: '2026-08-09T09:00:00Z',
  end: '2026-08-09T10:00:00Z',
  location: 'Studio A',
  organizer: 'Kiro Crew',
  attendees: [],
  description: '',
}]

let meetings = structuredClone(ORIGINAL_MEETINGS)

async function meetingsApi(path, route) {
  if (path === '/api/apps/meetings/config') {
    return json(route, { config: {} }), true
  }
  if (path === '/api/apps/meetings/calendar') {
    return json(route, { events: CALENDAR_EVENTS, provider: 'none', configured: false }), true
  }
  if (path === '/api/apps/meetings/meetings') {
    return json(route, { meetings }), true
  }
  if (path.startsWith('/api/apps/meetings/meetings/') && route.request().method() === 'DELETE') {
    const id = decodeURIComponent(path.slice('/api/apps/meetings/meetings/'.length))
    meetings = meetings.filter(meeting => meeting.event_id !== id)
    await route.fulfill({ status: 204 })
    return true
  }
  return false
}

async function openList(browser, base, theme) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })
  const page = await context.newPage()
  await stubDashboardApi(page, { theme, extra: meetingsApi })
  logPageProblems(page)
  await page.goto(base + '/meetings', { waitUntil: 'domcontentloaded' })
  await page.getByText('Weekly product sync').waitFor()
  return { context, page }
}

async function verifyListControls(page) {
  const available = page.getByRole('button', {
    name: 'Delete Weekly product sync',
    exact: true,
  })
  if (!(await available.isEnabled())) throw new Error('ended meeting delete action is not enabled')

  const protectedButtons = page.getByRole('button', {
    name: 'End this meeting before deleting it',
    exact: true,
  })
  if (await protectedButtons.count() !== 3) {
    throw new Error('active, paused, and reviewing rows must each expose a disabled delete action')
  }
  for (let index = 0; index < 3; index += 1) {
    if (!(await protectedButtons.nth(index).isDisabled())) {
      throw new Error('a protected meeting delete action is enabled')
    }
  }
  if (await page.getByRole('button', {
    name: 'Delete Tomorrow’s planning session',
    exact: true,
  }).count()) {
    throw new Error('calendar-only rows must not expose a local-data delete action')
  }
  return available
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    const dark = await openList(browser, base, 'dark')
    const available = await verifyListControls(dark.page)
    await available.hover()
    await dark.page.screenshot({
      path: `${OUT}/01-list-actions-dark.png`,
      fullPage: true,
    })

    let confirmation = ''
    dark.page.once('dialog', async dialog => {
      confirmation = dialog.message()
      await dialog.accept()
    })
    await available.click()
    await dark.page.getByText('Weekly product sync').waitFor({ state: 'detached' })
    if (!confirmation.includes('Weekly product sync') || !confirmation.includes('cannot be undone')) {
      throw new Error(`unexpected delete confirmation: ${confirmation}`)
    }
    await dark.page.getByRole('button', { name: 'Notifications', exact: true }).click()
    await dark.page.getByText('Deleted “Weekly product sync”').waitFor()
    await dark.page.screenshot({
      path: `${OUT}/02-after-delete-dark.png`,
      fullPage: true,
    })
    await dark.context.close()

    meetings = structuredClone(ORIGINAL_MEETINGS)
    const light = await openList(browser, base, 'light')
    const lightAvailable = await verifyListControls(light.page)
    await lightAvailable.hover()
    await light.page.screenshot({
      path: `${OUT}/03-list-actions-light.png`,
      fullPage: true,
    })
    await light.context.close()

    console.log('confirmation:', confirmation)
    console.log('wrote', `${OUT}/01-list-actions-dark.png`)
    console.log('wrote', `${OUT}/02-after-delete-dark.png`)
    console.log('wrote', `${OUT}/03-list-actions-light.png`)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(error => {
  console.error(error)
  process.exit(1)
})
