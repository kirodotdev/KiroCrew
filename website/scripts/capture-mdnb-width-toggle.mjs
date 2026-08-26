/**
 * Screenshot harness for the Notes app reading-column / full-width toggle.
 *
 * Captures five frames:
 *   01 - rendered note in the default 800px reading column (cap ON)
 *   02 - the same note at full viewport width (cap OFF, after clicking toggle)
 *   03 - raw Markdown editor at full width (the third surface that follows)
 *   04 - the reading column on a NARROW pane, where the centred column has no
 *        side margin left to absorb the floating header controls and the title
 *        reserves the overlap as right padding
 *   05 - the same column on the narrowest pane the desktop layout allows, where
 *        reserving would leave the title unreadably thin, so it drops below the
 *        controls instead
 *
 * Vault, note list, note document, API stub and the note-pane clip all come
 * from `lib/mdnb-fixtures.mjs`, the shared module every md-notebook harness
 * imports, so this file holds only what is about this feature: the note
 * content and the frame sequence.
 *
 * Uses a wide viewport (1600x900) so the difference between 800px and full
 * width is visually obvious. The fixture note contains a wide markdown table
 * and a long code line, the content that justifies the feature.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures. No gateway, no
 * dashboard token.
 *
 * Usage: node scripts/capture-mdnb-width-toggle.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import {
  MDNB_VAULT_ID,
  mdnbApiStub,
  mdnbNoteDoc,
  mdnbNotesList,
  notePaneClip,
} from './lib/mdnb-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mdnb-width-toggle'
mkdirSync(OUT, { recursive: true })

const VIEW = { width: 1600, height: 900 }
const NARROW = { width: 1024, height: 900 }
// Narrowest note pane the desktop layout allows: the side panel at its maximum
// inside the smallest non-mobile window, which leaves the pane about 348px.
const SQUEEZED = { width: 768, height: 900 }
const PANEL_MAX = 420

const NOTE_PATH = 'infrastructure-cost-comparison-across-all-deployment-regions-and-availability-zones-q3-2026.md'
const NOTE_TITLE = 'Infrastructure Cost Comparison - Q3 2026'

// A wide markdown table plus a long code line: the content that makes the
// reading-column versus full-width difference immediately visible.
const NOTE_CONTENT = `# ${NOTE_TITLE}

## Regional Cost Breakdown

| Region | EC2 Instances | RDS Storage (TB) | S3 Transfer (TB) | Lambda Invocations (M) | CloudFront (TB) | Total Monthly | YoY Growth | Projected Annual |
|--------|--------------|-------------------|-------------------|------------------------|-----------------|---------------|------------|-----------------|
| us-east-1 | $142,850.00 | $28,450.00 | $12,340.00 | $8,920.00 | $15,670.00 | $208,230.00 | +18.4% | $2,498,760.00 |
| eu-west-1 | $98,420.00 | $19,880.00 | $8,750.00 | $6,340.00 | $11,290.00 | $144,680.00 | +22.1% | $1,736,160.00 |
| ap-southeast-1 | $67,350.00 | $14,220.00 | $6,890.00 | $4,780.00 | $8,450.00 | $101,690.00 | +31.7% | $1,220,280.00 |
| us-west-2 | $54,180.00 | $11,650.00 | $5,430.00 | $3,920.00 | $7,120.00 | $82,300.00 | +15.2% | $987,600.00 |
| ap-northeast-1 | $45,670.00 | $9,870.00 | $4,560.00 | $3,210.00 | $6,340.00 | $69,650.00 | +27.8% | $835,800.00 |

## Deployment Configuration

\`\`\`bash
aws cloudformation deploy --template-file infrastructure/multi-region-stack.yaml --stack-name prod-global-2026q3 --parameter-overrides Environment=production VpcCidr=10.0.0.0/16 EnabledRegions=us-east-1,eu-west-1,ap-southeast-1,us-west-2,ap-northeast-1 AutoScalingMin=3 AutoScalingMax=48 InstanceType=m7g.2xlarge DatabaseEngine=aurora-postgresql DatabaseVersion=16.4 ReplicationMode=cross-region-active-active BackupRetentionDays=35 MonitoringLevel=detailed --capabilities CAPABILITY_NAMED_IAM --tags Project=InfraCost Environment=Production Quarter=Q3-2026 CostCenter=engineering-platform Owner=platform-team --no-fail-on-empty-changeset
\`\`\`

## Notes

The table above demonstrates why the full-width toggle matters: at 800px the table is clipped or requires horizontal scrolling, making cross-region comparisons difficult. At full pane width, all columns are visible simultaneously.
`

/** The toggle names the width in force, so each state has its own selector. */
const AT_COLUMN = 'button[aria-label="Medium width"]'
const AT_FULL = 'button[aria-label="Full width"][aria-pressed="true"]'

async function shot(page, file) {
  await page.screenshot({ path: `${OUT}/${file}`, clip: await notePaneClip(page) })
  console.log('wrote', `${OUT}/${file}`)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  try {
    const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 1, locale: 'en-US' })
    const page = await context.newPage()
    await stubDashboardApi(page, {
      theme: 'dark',
      extra: mdnbApiStub({
        notes: mdnbNotesList(NOTE_PATH, NOTE_TITLE),
        doc: mdnbNoteDoc(NOTE_PATH, NOTE_CONTENT),
      }),
    })
    logPageProblems(page)
    // Select the vault, and clear the width preference so frame 01 is the
    // default mode whatever a previous run left behind.
    await page.addInitScript(id => {
      localStorage.setItem('mdnb-active-vault', id)
      localStorage.removeItem('mdnb-full-width')
    }, MDNB_VAULT_ID)

    await page.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
    await page.getByText(NOTE_TITLE).first().waitFor({ timeout: 15000 })
    await page.getByText(NOTE_TITLE).first().click()
    await page.getByText('Regional Cost Breakdown').waitFor({ timeout: 15000 })
    await page.waitForTimeout(800)

    // Frame 1: the reading column, the default mode.
    const atColumn = page.locator(AT_COLUMN)
    await atColumn.waitFor({ timeout: 5000 })
    await shot(page, '01-reading-column.png')

    // Frame 2: full width. The control now names the width in force and reads
    // as pressed, which is what proves the state landed.
    await atColumn.click()
    const atFull = page.locator(AT_FULL)
    await atFull.waitFor({ timeout: 5000 })
    await page.waitForTimeout(400)
    await shot(page, '02-full-width.png')

    // Frame 3: the raw editor, the third surface that follows the width.
    await page.locator('button[aria-label="Markdown source"]')
      .or(page.locator('button[title="Markdown source"]')).first().click()
    await page.locator('textarea').first().waitFor({ timeout: 5000 })
    await page.waitForTimeout(400)
    await shot(page, '03-editor-full-width.png')

    // Frame 4: back to the rendered reading column, then shrink the viewport
    // until the pane has no side margin left to absorb the header controls.
    await page.locator('button[aria-label="Rendered"]')
      .or(page.locator('button[title="Rendered"]')).first().click()
    await page.waitForTimeout(300)
    await atFull.click()
    await page.setViewportSize(NARROW)
    await page.locator(AT_COLUMN).waitFor({ timeout: 5000 })
    await page.waitForTimeout(600)
    await shot(page, '04-reading-column-narrow.png')

    // Frame 5: the narrowest pane the app can produce in desktop layout, the
    // side panel dragged to its maximum in a 768px window. Here the cluster
    // cannot share the row at all, so the title takes its own line underneath.
    await page.evaluate(w => localStorage.setItem('mdnb-panel-width', String(w)), PANEL_MAX)
    await page.setViewportSize(SQUEEZED)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.getByText(NOTE_TITLE).first().waitFor({ timeout: 15000 })
    await page.getByText(NOTE_TITLE).first().click()
    await page.getByText('Regional Cost Breakdown').waitFor({ timeout: 15000 })
    await page.waitForTimeout(800)
    await shot(page, '05-title-stacked-narrow-pane.png')

    await context.close()
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
