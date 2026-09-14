/**
 * Screenshot harness + behaviour check for the folder-panel READ-FAILURE notices,
 * the header Refresh label, and the file rail kept mounted on a recoverable tree
 * failure.
 *
 * Drives the isolated capture entry (website/capture/folder-read-failure.html),
 * which mounts the real FolderPanel, FileBrowserRail and FilesHomePanel against
 * the real stylesheet and rejects each api-client seam with the exact error shape
 * production raises. Booting the full SPA is not needed for a notice, and a
 * half-stubbed shell photographs its own error boundary instead.
 *
 * This ASSERTS as well as photographs: every scene names the copy it must land on
 * and whether "Refresh to retry" may appear, and the run exits non-zero if a
 * frame does not match. That is what stops it becoming a screenshot generator
 * that quietly photographs the wrong state. Nothing in CI runs this file — the
 * CI-enforced half is FolderPanel.deadline.test.tsx, FolderPanel.tree.test.tsx,
 * FileBrowserRail.test.tsx and FilesHomePanel.test.tsx.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6811 --strictPort   # in another shell
 *   node scripts/capture-folder-read-failure.mjs http://127.0.0.1:6811 ../temp-screenshots/folder-read-failure
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6811'
const OUT = process.argv[3] || '../temp-screenshots/folder-read-failure'
mkdirSync(OUT, { recursive: true })

const fail = (msg) => { console.error(`FAIL ${msg}`); process.exitCode = 1 }
const ok = (msg) => console.log(`  ok  ${msg}`)

/**
 * `retry` names whether the notice may carry the "— Refresh to retry" clause.
 * Asserting the clause's ABSENCE is the half that matters: a refusal or a missing
 * root answers the same however often it is re-asked, so offering the control
 * there is the defect, and a frame alone cannot show that something is missing.
 */
const SCENES = [
  {
    id: '01-listing-timeout',
    scene: 'listing-timeout',
    says: 'Folder listing timed out',
    retry: true,
    note: 'listing arm, deadline — retryable, so the notice names the control',
  },
  {
    id: '02-listing-missing',
    scene: 'listing-missing',
    says: 'Folder not found',
    retry: false,
    note: 'listing arm, the endpoint\'s own not_a_directory code — a deleted folder is NOT retryable',
  },
  {
    id: '03-listing-denied',
    scene: 'listing-denied',
    says: 'No access to this folder',
    retry: false,
    note: 'listing arm, coded 403 — a refusal offers no remedy',
  },
  {
    id: '04-search-timeout',
    scene: 'search-timeout',
    says: 'Search timed out',
    retry: true,
    search: 'oauth',
    note: 'search arm, deadline — the one string naming the operation that expired',
  },
  {
    id: '05-search-denied',
    scene: 'search-denied',
    says: 'No access to this folder',
    retry: false,
    search: 'oauth',
    note: 'search arm, coded 403 — shares the listing arm string, no remedy',
  },
  {
    id: '06-stale-rows-under-failed-refetch',
    scene: 'stale-rows',
    says: 'Search timed out',
    retry: true,
    search: 'oauth',
    refresh: true,
    keepsRows: 'overview.md',
    note: 'a failed REFETCH keeps the rows it already had, under the notice',
  },
  {
    id: '07-tree-recoverable-folder-tab',
    scene: 'tree-recoverable',
    says: "Couldn't load the file tree — Refresh to retry",
    retry: true,
    keepsRows: 'README.md',
    note: 'tree failed, listing answered — names the TREE, and its remedy, above a listing that loaded',
  },
  {
    id: '08-one-notice-not-two',
    scene: 'one-notice-not-two',
    says: 'Folder listing timed out',
    retry: true,
    absent: "Couldn't load the file tree",
    note: 'ONE wedged gateway fails both reads — the tree notice is withheld',
  },
  {
    id: '09-rail-stays-mounted',
    scene: 'rail-recoverable',
    says: "Couldn't load the file tree — Refresh to retry",
    retry: true,
    namesRefreshControl: true,
    note: 'the rail stays MOUNTED with a notice instead of unmounting itself, and names its remedy',
  },
  {
    id: '10-files-home-recoverable',
    scene: 'files-home-recoverable',
    says: "Couldn't load the file tree — Refresh to retry",
    retry: true,
    namesRefreshControl: true,
    note: 'Files-home surface: the hint line goes silent while the rail names the failure and its remedy',
  },
  {
    id: '14-files-home-refusal',
    scene: 'files-home-refusal',
    says: 'No access to this folder',
    retry: false,
    note: 'Files-home, non-recoverable: the cause is NAMED and no labelled retry is offered for it',
  },
]

const RETRY_CLAUSE = 'Refresh to retry'

async function main() {
  // The browser resolves `libstdc++` before this repo's node does, and a node
  // shipped by a version manager can put an OLDER one first — the shell then dies
  // on a GLIBCXX symbol that the system library has. Pin the system paths for the
  // browser process only, so the harness runs on such a host.
  const libPath = ['/lib64', '/usr/lib64', process.env.LD_LIBRARY_PATH]
    .filter(Boolean).join(':')
  const browser = await chromium.launch({
    executablePath: chromiumExecutable(),
    env: { ...process.env, LD_LIBRARY_PATH: libPath },
  })

  // RECORD_VIDEO=1 records the Refresh MORPH instead of shooting the stills: the control
  // changes form in place (26px icon -> icon+label) and the reserved-width design claims the
  // growth is spent while the read is still in flight, which no pair of stills can show.
  // Convert the clip with (1.3s drops the boot frames; before that the page has not painted):
  //   ffmpeg -ss 1.3 -i <out>/video-raw/*.webm -an -c:v libx264 -pix_fmt yuv420p \
  //     -crf 30 -vf scale=840:-2 <out>/13-refresh-morph.mp4
  //   ffmpeg -ss 1.3 -i <webm> -vf "fps=10,scale=560:-1:flags=lanczos,\
  //     palettegen=stats_mode=diff:max_colors=128" -f image2 <out>/pal.png
  //   ffmpeg -ss 1.3 -i <webm> -i <out>/pal.png -lavfi "fps=10,scale=560:-1:flags=lanczos[v];\
  //     [v][1:v]paletteuse=dither=bayer:bayer_scale=5" <out>/13-refresh-morph.gif
  if (process.env.RECORD_VIDEO === '1') {
    const context = await browser.newContext({
      viewport: { width: 460, height: 400 },
      recordVideo: { dir: `${OUT}/video-raw`, size: { width: 460, height: 400 } },
    })
    const page = await context.newPage()
    await page.goto(`${BASE}/capture/folder-read-failure.html?scene=refresh-morph&theme=dark`)
    await page.getByText('README.md', { exact: false }).first().waitFor()
    await page.waitForTimeout(900)
    await page.getByLabel('Refresh').click()
    await page.getByText(/Folder listing timed out/).first().waitFor({ timeout: 15_000 })
    await page.waitForTimeout(1200)
    const clip = await page.video()?.path()
    await context.close()
    await browser.close()
    console.log(`WEBM ${clip}`)
    console.log('NOTE: clip mode -- stills and assertions SKIPPED. Run without RECORD_VIDEO=1 to verify.')
    return
  }
  const context = await browser.newContext({
    viewport: { width: 900, height: 600 },
    deviceScaleFactor: 2, // 11-13px notice type renders soft at 1x
  })

  for (const s of SCENES) {
    const page = await context.newPage()
    page.on('pageerror', e => fail(`${s.id}: page error ${e.message}`))
    await page.goto(`${BASE}/capture/folder-read-failure.html?scene=${s.scene}&theme=dark`)

    if (s.search) {
      const box = page.getByLabel('Search files')
      await box.waitFor()
      await box.fill(s.search)
    }
    if (s.refresh) {
      // The FIRST answer has landed; the refetch is what fails, which is the
      // only way to reach "rows retained under a failed refetch".
      await page.getByText(s.keepsRows, { exact: false }).first().waitFor()
      await page.getByLabel('Refresh').click()
    }

    const notice = page.getByText(new RegExp(s.says.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
    try {
      await notice.first().waitFor({ timeout: 15_000 })
      ok(`${s.id}: "${s.says}" — ${s.note}`)
    } catch {
      fail(`${s.id}: never rendered "${s.says}"`)
      await page.screenshot({ path: `${OUT}/${s.id}-MISSING.png` })
      await page.close()
      continue
    }

    const body = await page.locator('body').innerText()
    const hasRetry = body.includes(RETRY_CLAUSE)
    if (s.retry && !hasRetry) fail(`${s.id}: retryable cause did not name "${RETRY_CLAUSE}"`)
    if (!s.retry && hasRetry) fail(`${s.id}: unretryable cause offered "${RETRY_CLAUSE}"`)
    if (s.absent && body.includes(s.absent)) fail(`${s.id}: "${s.absent}" should have been withheld`)
    if (s.keepsRows && !body.includes(s.keepsRows)) fail(`${s.id}: lost the rows it should have kept (${s.keepsRows})`)
    if (s.namesRefreshControl) {
      const label = (await page.getByLabel('Refresh').first().innerText()).trim()
      if (label !== 'Refresh') fail(`${s.id}: the control the notice names carries no label (got ${JSON.stringify(label)})`)
    }

    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${s.id}.png` })
    await page.close()
  }

  // The header Refresh, before and after: a 26px icon-only control against the
  // grown, labelled one. Two frames of the SAME control, because the change is a
  // width morph and one frame cannot show a difference.
  const page = await context.newPage()
  await page.goto(`${BASE}/capture/folder-read-failure.html?scene=idle&theme=dark`)
  const header = page.getByLabel('Refresh')
  await header.waitFor()
  await page.getByText('README.md', { exact: false }).first().waitFor()
  if ((await header.innerText()).trim() !== '') {
    fail('11-refresh-idle: the idle control should carry no visible label')
  } else {
    ok('11-refresh-idle: icon-only while no retryable failure is plausible')
  }
  await header.screenshot({ path: `${OUT}/11-refresh-idle.png` })
  await page.close()

  const page2 = await context.newPage()
  await page2.goto(`${BASE}/capture/folder-read-failure.html?scene=listing-timeout&theme=dark`)
  const header2 = page2.getByLabel('Refresh')
  await header2.waitFor()
  await page2.getByText(/Folder listing timed out/).first().waitFor({ timeout: 15_000 })
  if ((await header2.innerText()).trim() !== 'Refresh') {
    fail(`12-refresh-named: expected the label to read "Refresh", got ${JSON.stringify((await header2.innerText()).trim())}`)
  } else {
    ok('12-refresh-named: label revealed while a retryable failure is plausible')
  }
  await header2.screenshot({ path: `${OUT}/12-refresh-named.png` })
  await page2.close()

  await browser.close()
  console.log(process.exitCode ? '\nSOME SCENES FAILED' : `\nall scenes ok -> ${OUT}`)
}

main()
