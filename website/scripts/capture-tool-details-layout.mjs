/**
 * Screenshot + geometry assertions for capture/tool-details-layout.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6823 --strictPort &
 *   node scripts/capture-tool-details-layout.mjs http://127.0.0.1:6823 \
 *     ../temp-screenshots/tool-details-layout
 *
 * The assertions carry the proof, not the image. "Looks tidier" is not a
 * verdict a frame can deliver, but "the meta row occupies one line", "the chip
 * text and the payload table text share a left edge", and "a section control
 * only appears when there are two sections" all are — and each is exactly the
 * defect the layout change targets, so a regression fails the run instead of
 * quietly producing a plausible picture.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6823'
const OUT = process.argv[3] || '../temp-screenshots/tool-details-layout'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

const check = (name, ok, detail) => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failed++
}

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({
    viewport: { width: 800, height: 900 },
    // deviceScaleFactor 1, not 2: the frame stack is tall enough that 2x lands
    // within ~15px of the 2000px-per-edge capture cap, and an oversized image is
    // rejected wholesale rather than downscaled.
    deviceScaleFactor: 1,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))

  await page.goto(`${BASE}/capture/tool-details-layout.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
  // framer-motion's height:auto entrance and the layoutId pill spring both
  // settle within a few hundred ms; measuring mid-animation reads a transient
  // height and would make the one-line assertion flap.
  await page.waitForTimeout(900)

  const probe = await page.evaluate(() => {
    const out = {}
    const SECTIONS = ['Input', 'Output']
    const MODES = ['Formatted', 'Raw']
    for (const state of ['pending', 'both', 'rawlabel', 'narrow', 'ghost']) {
      const host = document.querySelector(`[data-state="${state}"]`)
      if (!host) continue
      // The panel root is the bar-railed flex column ToolDetails renders.
      const panel = host.firstElementChild
      const rows = [...panel.children]
      const metaRow = rows[0]
      const chip = metaRow.querySelector('span[title]')
      const chipIcon = chip?.querySelector('svg')
      // Panel children: meta row, then (when there is a payload) the bare
      // controls row, then the payload box.
      const controls = rows.length > 1 ? rows[1] : null
      // framer-motion wraps each pane in an animating div; the bordered payload
      // box is that wrapper's child, so descend one level or every measurement
      // is taken against a frameless shell.
      const shell = rows[rows.length - 1]
      const block = shell.firstElementChild || shell
      // Plain-text payloads render as a <pre> (which IS the box), JSON objects as
      // a <table> inside the padded box.
      const firstCell = block.querySelector('table td') || (block.tagName === 'PRE' ? block : null)
      const btns = controls
        ? [...controls.querySelectorAll('button')].map(b => ({
            label: (b.textContent || '').trim(),
            color: getComputedStyle(b).color,
          }))
        : []
      const stripKids = controls ? [...controls.children] : []
      const left = el => (el ? +el.getBoundingClientRect().left.toFixed(1) : null)
      // The <pre> owns the body padding; a <td> sits inside a padded div. Add the
      // element's own left padding so both report the same inset.
      const padLeft = el => (el ? parseFloat(getComputedStyle(el).paddingLeft) || 0 : 0)
      const midY = el => {
        const r = el.getBoundingClientRect()
        return r.top + r.height / 2
      }
      // "No frame around the pair": the controls row itself must draw neither a
      // border nor a fill, or the panel is boxes inside boxes again.
      const cs = controls ? getComputedStyle(controls) : null
      out[state] = {
        metaRowHeight: Math.round(metaRow.getBoundingClientRect().height),
        metaRowHasAutoMargin: [...metaRow.querySelectorAll('*')].some(
          el => getComputedStyle(el).marginLeft === 'auto',
        ),
        controlsRowIsBare: cs
          ? parseFloat(cs.borderTopWidth) === 0 &&
            parseFloat(cs.borderBottomWidth) === 0 &&
            parseFloat(cs.borderLeftWidth) === 0 &&
            parseFloat(cs.borderRightWidth) === 0 &&
            (cs.backgroundColor === 'rgba(0, 0, 0, 0)' || cs.backgroundColor === 'transparent')
          : null,
        // Frame depth inside the payload box: only the box itself may draw an
        // outline. Table row rules are borders too, so count only descendants
        // that ring all four sides.
        nestedBoxes: [...block.querySelectorAll('*')].filter(el => {
          const s = getComputedStyle(el)
          return ['Top', 'Right', 'Bottom', 'Left'].every(
            side => parseFloat(s[`border${side}Width`]) > 0,
          )
        }).length,
        hasChip: !!chip,
        chipWidth: chip ? Math.round(chip.getBoundingClientRect().width) : null,
        // Box edges: the chip and the payload block are siblings in the railed
        // column, so they must start on the same x.
        chipBoxLeft: left(chip),
        blockBoxLeft: left(block),
        // Inner insets: the first glyph inside each box, measured from that box's
        // own outer edge. The chip leads with an icon, so its TEXT can never line
        // up with the table's — what has to agree is the padding.
        chipInset: chip && chipIcon ? +(left(chipIcon) - left(chip)).toFixed(1) : null,
        cellInset: firstCell
          ? +(firstCell === block
              ? parseFloat(getComputedStyle(block).borderLeftWidth) + padLeft(block)
              : left(firstCell) - left(block)
            ).toFixed(1)
          : null,
        sectionButtons: btns.filter(b => SECTIONS.includes(b.label)),
        modeButtons: btns.filter(b => MODES.includes(b.label)),
        stripChildCount: stripKids.length,
        // Vertical CENTRES, not tops: a text label and a capsule have different
        // heights, so equal tops would mean they are NOT centred together.
        stripSameRow:
          stripKids.length < 2
            ? true
            : Math.abs(midY(stripKids[0]) - midY(stripKids[stripKids.length - 1])) < 2,
        // How many distinct rows the controls occupy, by counting distinct tops.
        stripRows: new Set(stripKids.map(k => Math.round(k.getBoundingClientRect().top))).size,
        // Clipping: does any control reach past its own row's content box? This is
        // what a missing `flex-wrap` produces, and it is invisible in a wide frame.
        stripOverflowPx: controls
          ? Math.max(
              0,
              ...stripKids.map(k =>
                Math.round(
                  k.getBoundingClientRect().right - controls.getBoundingClientRect().right,
                ),
              ),
              0,
            )
          : 0,
        // Opposite ends of the strip: the gap between the two controls must be
        // the slack `justify-between` pushes out, not a fixed flex gap.
        stripGap:
          stripKids.length === 2
            ? Math.round(
                stripKids[1].getBoundingClientRect().left -
                  stripKids[0].getBoundingClientRect().right,
              )
            : null,
        labelText: stripKids[0]?.tagName === 'SPAN' ? stripKids[0].textContent.trim() : null,
      }
    }
    return out
  })

  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}.png` })

  console.log(`\n--- ${theme} ---`)
  check(`${theme}: no page errors`, errors.length === 0, errors[0])

  // The narrow frame is deliberately too small for both capsules side by side, so
  // the one-line and same-row expectations below do not apply to it — it gets its
  // own block further down.
  for (const [state, d] of Object.entries(probe).filter(([s]) => s !== 'narrow')) {
    // A single chip row is ~19px tall; a wrapped row doubles that. This is the
    // reported defect stated as a number.
    check(`${theme}/${state}: meta row is one line`, d.metaRowHeight <= 24, `${d.metaRowHeight}px`)
    check(`${theme}/${state}: no auto-margin island in the meta row`, !d.metaRowHasAutoMargin)
    if (d.hasChip) {
      check(`${theme}/${state}: display title is capped`, d.chipWidth <= 322, `${d.chipWidth}px`)
      check(
        `${theme}/${state}: chip and payload block share a left edge`,
        Math.abs(d.chipBoxLeft - d.blockBoxLeft) < 1.5,
        `chip ${d.chipBoxLeft} vs block ${d.blockBoxLeft}`,
      )
      check(
        `${theme}/${state}: chip and payload use the same inner inset`,
        Math.abs(d.chipInset - d.cellInset) < 1.5,
        `chip ${d.chipInset} vs cell ${d.cellInset}`,
      )
    }
    check(`${theme}/${state}: strip controls sit on one row`, d.stripSameRow)
  }

  // These hold at every width, narrow frame included.
  for (const [state, d] of Object.entries(probe)) {
    check(`${theme}/${state}: controls row draws no frame of its own`, d.controlsRowIsBare === true)
    // The payload box is the only frame. A capsule ringing all four sides inside
    // it would mean a box inside a box.
    check(`${theme}/${state}: nothing framed inside the payload box`, d.nestedBoxes === 0, `${d.nestedBoxes} nested`)
    check(`${theme}/${state}: no control clipped past the row`, d.stripOverflowPx === 0, `${d.stripOverflowPx}px over`)
  }

  // Narrow container: both controls must survive by WRAPPING. Without `flex-wrap`
  // the capsules cannot shrink (inline-flex, default min-width:auto) and the
  // render-mode toggle is clipped away instead.
  const n = probe.narrow
  check(
    `${theme}/narrow: both controls still rendered`,
    n.sectionButtons.length === 2 && n.modeButtons.length === 2,
    `section=${n.sectionButtons.length} mode=${n.modeButtons.length}`,
  )
  check(`${theme}/narrow: controls wrap onto two rows`, n.stripRows === 2, `${n.stripRows} row(s)`)

  // The approval bar's compact ghost: bare by contract. One section means no
  // section control at all here -- not a toggle and not a naming label -- while
  // the render-mode toggle survives, since a JSON input is worth reading raw
  // before approving it.
  const g = probe.ghost
  check(
    `${theme}/ghost: no section control at all`,
    g.sectionButtons.length === 0 && g.labelText === null,
    `buttons=${g.sectionButtons.length} label=${g.labelText}`,
  )
  check(`${theme}/ghost: render-mode toggle still offered`, g.modeButtons.length === 2)

  // Raw-label mode: the pill already shows the display title, so the chip would
  // repeat it. Its absence here is the existing dedup working, not a miss.
  check(`${theme}/rawlabel: no chip when it would duplicate the pill`, probe.rawlabel.hasChip === false)

  const p = probe.pending
  check(
    `${theme}/pending: one section → label, no segment buttons`,
    p.sectionButtons.length === 0 && p.labelText === 'Input',
    `buttons=${p.sectionButtons.length} label=${p.labelText}`,
  )
  // The render-mode toggle is independent of the section count: a JSON input is
  // still worth reading raw while it awaits approval.
  check(`${theme}/pending: render-mode toggle still offered`, p.modeButtons.length === 2)

  const b = probe.both
  check(
    `${theme}/both: two sections → a real toggle`,
    b.sectionButtons.length === 2,
    b.sectionButtons.map(x => x.label).join(','),
  )
  // State is carried by text colour, which is what survives light themes where
  // --card and --bg-elevated are the same value.
  const colours = new Set(b.sectionButtons.map(x => x.color))
  check(`${theme}/both: active segment is visually distinct`, colours.size >= 2, [...colours].join(' | '))
  // Plain-text output has no formatted/raw distinction, so the strip carries
  // the section control alone.
  check(`${theme}/both: no render-mode toggle for plain-text output`, b.modeButtons.length === 0)

  const r = probe.rawlabel
  check(
    `${theme}/rawlabel: both controls present, pushed to opposite ends`,
    r.sectionButtons.length === 2 && r.modeButtons.length === 2 && r.stripGap > 40,
    `gap=${r.stripGap}px`,
  )

  const dim = await page.locator('[data-capture-root]').boundingBox()
  check(`${theme}: frame within the 2000px capture cap`, dim.width <= 2000 && dim.height <= 2000, `${Math.round(dim.width)}x${Math.round(dim.height)}`)

  await ctx.close()
}

await browser.close()
console.log(failed ? `\n${failed} check(s) FAILED` : '\nall checks passed')
process.exit(failed ? 1 : 0)
