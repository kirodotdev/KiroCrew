/**
 * The render gate's verdict: which findings FAIL, and which are only reported.
 *
 * ## Only a diff can fail
 *
 * Two things are measured on every run and they are not the same kind of number:
 *
 *   - **a diff** — this tree's per-surface count against the count measured on
 *     another commit's tree with the same scanner (`[vs-base]`). Both sides are
 *     derived at runtime; nothing is read from a committed file. A diff answers
 *     "did THIS change make it worse", which is a question with one right answer.
 *   - **a total** — this tree's per-surface count against a number stored in
 *     `src/i18n/render-baseline.json`. A total answers "is the repo's absolute debt
 *     above the figure someone last wrote down", which is a question about the
 *     REPO, not about the change in front of it.
 *
 * Only the diff fails. The total is a report — with one exception, below.
 *
 * That is not a loosening — it is the fix for a defect the totals had. A stored
 * total is written by whichever branch measured it last, so two branches that never
 * touch the same file can still combine into a number neither of them produced.
 * That happened: #1107 recorded `projects.latent: 16` measured on its own base,
 * #985 independently added one `truncate` span with no `min-w-0` to the Projects
 * rail, and once both merged `main` measured 24 against a ledger of 16 and went red
 * — on a defect that neither branch's diff introduced, with no diff anywhere to
 * point at. Nobody could fix that by fixing their own change, which is the
 * signature of a gate asking the wrong question.
 *
 * The diff loses nothing in exchange, because every increment reaches `main`
 * through some diff: a PR is measured against its base, and a push to `main` is
 * measured against the commit it replaced (`ci.yml` passes `github.event.before`).
 * The interaction above IS caught — as a `main` push diff of `16 -> 24` with the
 * merge that produced it named — it just is not charged to an unrelated PR.
 *
 * ## The exception: with no diff, the total is what is left
 *
 * `totalIsFallback` covers the run that HAS no base — not the run that declined one
 * (`--no-vs-base`, `--surface`, `--update` all asked for a report and keep getting
 * one). There the total fails, because the alternative is exiting 0 having checked
 * nothing but DNT, and a gate that stops guarding silently is worse than a gate
 * asking a slightly wrong question. The weaker check is still a check.
 *
 * DNT violations stay zero-tolerance and are not a count at all.
 *
 * Split out of `check-i18n-render.mjs` so this decision is a pure function over
 * plain numbers, unit-tested directly, rather than a branch reachable only by
 * building two bundles and driving a browser for ten minutes.
 */

/**
 * Ledger buckets.
 *
 *   text   — never reached a catalog, or was assembled from several keys.
 *   layout — the text does not fit RIGHT NOW, at the width the pseudolocale renders.
 *            Since the locale set shrank to `en-XA` + a DNT probe (see `LOCALES`),
 *            this bucket is no longer graded against a real script, so it
 *            over-reports: a site flagged here may fit in every locale we ship.
 *   latent — a pattern that is silently broken but not yet visible, which today
 *            means `ellipsis-with-flex-parent`: `text-overflow` cannot apply while
 *            a flex child's `min-width` is `auto`, so the truncation those sites
 *            think they have does nothing and the text pushes its sibling out
 *            instead. It is kept separate because there are ~460 of them and
 *            folding them into `layout` would bury the handful of real overflows
 *            under a uniform `min-w-0` cleanup that belongs to its own PR.
 */
export const BUCKETS = ['text', 'layout', 'latent']

/**
 * Signatures that are LATENT — broken now, visible later.
 *
 * A set, not `signature === 'ellipsis-with-flex-parent'`, because the bucket is a
 * CATEGORY and the equality test made it a single hard-coded name: the next latent
 * pattern would have landed in `layout` and buried the handful of real overflows
 * under it, which is the exact outcome this bucket exists to prevent. Membership is
 * unchanged today — one signature — so no count moves.
 */
export const LATENT_SIGNATURES = new Set(['ellipsis-with-flex-parent'])

/**
 * What each bucket means, for the log.
 *
 * The gate speaks two vocabularies — SIGNATURES (`latin-leak/untranslated-text`) and
 * BUCKETS (`text`) — and every number it enforces or records is bucketed while the
 * only actionable breakdown is by signature. Printing the counts without the mapping
 * left the reader unable to derive `text=1728` from `1366 untranslated-text`, and
 * left `layout`'s absence from a drift list ambiguous between "accurate", "not
 * measured" and "no such key". The grouping is stated so neither has to be guessed.
 */
export const BUCKET_MEANING = {
  text: 'catalog work — never reached a catalog, or glued from several keys',
  latent: 'silently broken, not yet visible — one uniform `min-w-0` sweep',
  layout: 'does not fit RIGHT NOW at pseudolocale width — CSS, case by case',
}

/**
 * The `_comment` `--update` writes into `src/i18n/render-baseline.json`.
 *
 * It lives here, beside the rule it describes, for two reasons. A regen has to
 * reproduce the committed file byte-for-byte — the writer's wording and the
 * checked-in wording had already drifted apart, so the next `--update` would have
 * churned a paragraph nobody edited. And the file's own doctrine is now the only
 * thing standing between a reader and a number that fails nothing: `renderVerdict`
 * asserts the committed `_comment` equals this string, so the record cannot end up
 * describing a rule the code stopped following.
 */
export const LEDGER_COMMENT = 'Phase 5 render-time gate. Findings from rendering the real '
  + 'DEV build under en-XA -- all three buckets, including layout/latent, so those are '
  + 'measured at PSEUDOLOCALE width rather than in any locale we ship. The one real locale '
  + 'in the set is a DNT probe whose other findings are discarded, so it contributes '
  + 'nothing here; see LOCALES in scripts/lib/i18n-surfaces.mjs for what that trade cost. '
  + 'This is a DEBT '
  + 'RECORD, not the gate: [vs-base] renders the base commit and fails on any per-surface '
  + 'increase without reading this file, because a total is written by whichever branch '
  + 'measured it last and so cannot be attributed to any one diff -- see '
  + 'scripts/lib/render-verdict.mjs. These numbers decide a run in exactly one case: a run '
  + 'that had NO base commit to diff, where the alternative is checking nothing. Regenerate '
  + 'with `node scripts/check-i18n-render.mjs --build --update` whenever it drifts; the goal '
  + 'for every entry is 0. DNT findings are not recorded here -- they are zero-tolerance.'

export const bucketOf = finding => {
  if (finding.kind !== 'layout') return 'text'
  return LATENT_SIGNATURES.has(finding.signature) ? 'latent' : 'layout'
}

/**
 * A finding's identity across two renders, so a failing run can name the findings a
 * branch ADDED rather than only the count it moved.
 *
 * Keyed on source location when there is one. That is what makes the diff usable:
 * the DOM path is positional (`div:nth(1)>div>div:nth(1)>button:nth(0)`), so inserting
 * one wrapper renumbers siblings and every finding below it reads as "new". A
 * `file:line` only changes when someone edits that file, which is the thing being
 * attributed. Where no source is available the path is the fallback and the caller
 * marks the result approximate.
 */
export const identityOf = ({ surface, finding }) => [
  surface,
  finding.signature,
  finding.source && finding.source.length ? finding.source.join('<') : `path:${finding.path}`,
  finding.detail || finding.text || '',
].join('|')

/** Whether an identity set can be trusted to attribute findings to a diff. */
export const identityIsExact = records => records.every(
  r => r.finding.source && r.finding.source.length,
)

export const zeroCounts = () => Object.fromEntries(BUCKETS.map(b => [b, 0]))

/** Group raw findings into per-surface bucket counts, splitting DNT out. */
export function tally(records, surfaceIds) {
  const counts = {}
  for (const id of surfaceIds) counts[id] = zeroCounts()
  const dnt = []
  for (const { surface, locale, finding } of records) {
    if (finding.kind === 'dnt') {
      dnt.push({ surface, locale, finding })
      continue
    }
    if (!counts[surface]) counts[surface] = zeroCounts()
    counts[surface][bucketOf(finding)] += 1
  }
  return { counts, dnt }
}

export const totals = counts => Object.fromEntries(
  BUCKETS.map(b => [b, Object.values(counts).reduce((n, x) => n + (x[b] || 0), 0)]),
)

/**
 * Decide the run.
 *
 * @param {object}   a
 * @param {object}   a.counts       per-surface bucket counts for this tree
 * @param {object?}  a.baseCounts   the same, measured on the base tree — `null` when
 *                                  there was no base to diff against
 * @param {object?}  a.ledger       parsed `render-baseline.json`, or `null`
 * @param {string[]} a.surfaceIds   every surface the ledger is keyed by
 * @param {object[]} a.dnt          DNT findings (zero tolerance)
 * @param {boolean}  a.partial      a `--surface`/`--locale` run, which under-counts
 * @param {string}   a.onlySurface  the `--surface` id, when partial
 * @param {Set<string>} a.newSurfaces  surfaces this branch added, whose base count is
 *                                  forced to 0 because no base measurement exists — the
 *                                  base bundle has no route for them, so what its sweep
 *                                  reported for that URL is a fallback page, not them.
 * @param {boolean}  a.totalIsFallback  no base was AVAILABLE (as opposed to a caller
 *                                  that asked not to diff). With no diff to fail on,
 *                                  the total is all that is left, so it becomes the
 *                                  guard for that run instead of a report — otherwise
 *                                  the run would exit 0 having checked nothing but DNT.
 * @returns {{failed: boolean, enforced: boolean, dnt: object[], grew: object[],
 *            shrank: object[], above: object[], below: object[]}}
 */
export function decide({
  counts,
  baseCounts = null,
  ledger = null,
  surfaceIds,
  dnt = [],
  partial = false,
  onlySurface = '',
  totalIsFallback = false,
  newSurfaces = new Set(),
}) {
  const grew = []
  const shrank = []
  if (baseCounts) {
    for (const id of surfaceIds) {
      for (const bucket of BUCKETS) {
        const now = counts[id]?.[bucket] || 0
        // A surface whose ROUTE this branch added has no base measurement. The base
        // bundle had no route for its URL, so the base sweep was redirected and
        // reported whatever fallback page it landed on — which can be BUSIER than
        // the new surface, making its real defects read as an improvement and
        // passing the branch that shipped them. Zero is the honest base for a
        // surface that did not exist, so every finding it brings is growth.
        //
        // `newSurfaces` therefore means "the base had no route for this URL",
        // proven by the redirect, and NOT "this id is absent from the base
        // registry". Registering a surface whose route already existed must compare
        // against its real base count: its findings are pre-existing debt the
        // registration merely revealed, and charging them to the registering branch
        // is what made widening the gate's aperture impossible. See
        // `check-i18n-render.mjs` for how the distinction is measured.
        const was = newSurfaces.has(id) ? 0 : (baseCounts[id]?.[bucket] || 0)
        if (now > was) grew.push({ surface: id, bucket, was, now })
        else if (now < was) shrank.push({ surface: id, bucket, was, now })
      }
    }
  }

  const above = []
  const below = []
  if (ledger) {
    for (const id of surfaceIds) {
      if (partial && onlySurface && id !== onlySurface) continue
      const ceiling = (ledger.surfaces && ledger.surfaces[id]) || zeroCounts()
      for (const bucket of BUCKETS) {
        const now = counts[id]?.[bucket] || 0
        const was = ceiling[bucket] || 0
        // A partial run cannot see every locale, so it can only ever under-count.
        // Reporting a "decrease" from that would invite a bogus ledger drop.
        if (partial && now < was) continue
        if (now > was) above.push({ surface: id, bucket, was, now })
        else if (now < was) below.push({ surface: id, bucket, was, now })
      }
    }
  }

  // Whether anything was actually enforced this run. A run with no base diffed
  // nothing, so it must say so loudly instead of exiting 0 as if it had checked.
  const enforced = !!baseCounts

  return {
    dnt,
    grew,
    shrank,
    above,
    below,
    enforced,
    // `above` fails ONLY as a fallback — when there was no diff to be had. With a
    // diff in hand a total is a report, because it is not attributable to one (see
    // the header); `!enforced` is what keeps that true even if a caller passes
    // `totalIsFallback` alongside a base. With no diff, a report that nothing can
    // fail is not a gate at all, and "exit 0 having checked nothing" is how the
    // render gate would stop guarding without anyone noticing. So the weaker check
    // runs rather than none.
    failed: dnt.length > 0 || grew.length > 0
      || (!enforced && totalIsFallback && above.length > 0),
  }
}

export const fmtDelta = ({ surface, bucket, was, now }) =>
  `${surface}.${bucket}: ${was} -> ${now}${now > was ? ` (+${now - was})` : ''}`

/**
 * The findings present at HEAD and absent at base — what this branch ADDED.
 *
 * `[vs-base]` fails on a per-surface COUNT, which tells the author a number moved and
 * nothing about which code moved it. `chat.latent: 48 -> 64 (+16)` used to be the end
 * of the message; printing all 64 would not have helped either. This narrows it to the
 * 16, each with the `file:line` that produced it.
 *
 * Multiset, not set: sixteen identical findings from one `.map()` are sixteen
 * findings, and collapsing them to one would under-report the work. Restricted to the
 * surface/bucket pairs that actually grew, so a branch that fixes one surface while
 * another shifts internally does not get handed unrelated churn.
 */
export function addedFindings(headRecords, baseRecords, grew) {
  const grewKeys = new Set(grew.map(g => `${g.surface}|${g.bucket}`))
  if (!grewKeys.size) return []
  const remaining = new Map()
  for (const r of baseRecords) {
    const k = identityOf(r)
    remaining.set(k, (remaining.get(k) || 0) + 1)
  }
  const added = []
  for (const r of headRecords) {
    if (!grewKeys.has(`${r.surface}|${bucketOf(r.finding)}`)) continue
    const k = identityOf(r)
    const left = remaining.get(k) || 0
    if (left > 0) { remaining.set(k, left - 1); continue }
    added.push(r)
  }
  return added
}
