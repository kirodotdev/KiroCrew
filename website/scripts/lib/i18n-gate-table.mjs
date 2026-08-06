/**
 * The i18n gate table, and the decision over it — a pure function so the exit code
 * can be unit-tested instead of discovered in CI.
 *
 * ## Why this is a separate module
 *
 * The first version of the runner lived entirely in `i18n-check.mjs`, and it shipped a
 * hole that two independent reviewers found and reproduced: a script that exited
 * non-zero BEFORE printing any line a row recognised made every row it owns `NOT RUN`,
 * `NOT RUN` was excluded from the failure set, and the `unexplained` guard written for
 * exactly that case was switched off by the very rows it should have caught. The runner
 * exited 0 where the `&&` chain it replaced exited 2 — eslint failing to load, an
 * unresolvable `I18N_BASE_REF`, a catalog that did not parse. A gate that stops
 * guarding silently is the defect this whole change exists to remove, and it was
 * reachable because the verdict was a chain of filters nothing could test.
 *
 * So the verdict is a function over plain data, and `i18nGateTable.test.ts` pins the
 * invariant that matters: **a non-zero child always fails the run**, whatever the rows
 * say. Row states are for ATTRIBUTION — telling the author which check to look at —
 * never for deciding whether the step passes.
 */

/** @typedef {'PASS'|'FAIL'|'MISSING'|'NOT RUN'} RowState */

/** The eight processes. Each may host more than one check. */
export const SCRIPTS = [
  { key: 'pseudo', argv: ['gen-pseudolocale.mjs', '--check'] },
  { key: 'keys', argv: ['check-i18n-keys.mjs'] },
  { key: 'codemod', argv: ['i18n-codemod.mjs', '--check', '--baseline=3'] },
  { key: 'plural', argv: ['i18n-plural-codemod.mjs', '--check'] },
  { key: 'source', argv: ['check-source-strings.mjs'] },
  { key: 'strings', argv: ['check-i18n-strings.mjs'] },
  { key: 'dnt', argv: ['check-dnt-catalogs.mjs'] },
  { key: 'manifest', argv: ['check-app-manifest-sync.mjs'] },
]

/**
 * The thirteen checks, in reading order within their section.
 *
 * `find` pulls the numbers out of the owning script's output on the PASSING path;
 * `whenFailed` does the same for the failing path, because most of these scripts print
 * a different sentence when they fail and `find` would simply not match.
 *
 * `ok` decides the row from the captured numbers; omit it and the row inherits the
 * script's exit code, which is right for a script hosting exactly one check.
 *
 * `enforce: 'info'` marks a whole-repo measurement that CANNOT fail the step. Those
 * scripts print a `[<id>] REPORT:` line instead of exiting non-zero, so an info row is
 * PASS whenever it was measured at all — see `resolveRows`.
 *
 * `scope: 'diff'` also means "needs a base commit": with none supplied those checks do
 * not run at all, and the table says so rather than calling them broken.
 */
export const CHECKS = [
  // ---- diff-scoped: yours, zero tolerance, no number to raise ----------------
  {
    id: 'added-lines', script: 'strings', scope: 'diff', enforce: 'zero',
    find: /^\[added-lines\] (\d+) untranslated/m,
    ok: m => m[1] === '0',
    summary: m => `${m[1]} untranslated literal(s) on lines you wrote`,
  },
  {
    id: 'vs-base', script: 'strings', scope: 'diff', enforce: 'zero',
    find: /^\[vs-base\] (\d+) touched file\(s\) gained/m,
    ok: m => m[1] === '0',
    summary: m => `${m[1]} touched file(s) got worse than the base`,
  },
  {
    id: 'source-strings', script: 'source', scope: 'diff', enforce: 'zero',
    find: /^\[source-strings\] (\d+) new key\(s\)[^,]*, (\d+) finding/m,
    ok: m => m[2] === '0',
    summary: m => `${m[1]} new English key(s) · ${m[2]} badly shaped`,
  },
  {
    id: 'changed-values', script: 'source', scope: 'diff', enforce: 'zero',
    find: /^\[changed-values\] (\d+) catalog QA finding/m,
    ok: m => m[1] === '0',
    summary: m => `${m[1]} QA finding(s) among catalog values you added or changed`,
  },
  // ---- whole-repo: may be inherited, but can still fail this step ------------
  {
    id: 'key-refs', script: 'keys', scope: 'repo', enforce: 'hard-zero',
    find: /^OK: (\d+) static key references all resolve/m,
    summary: m => `${m[1]} references resolve · 0 dangling`,
    whenFailed: {
      find: /^(\d+) translation key reference\(s\) do not exist/m,
      summary: m => `${m[1]} reference(s) name a key that does not exist`,
    },
  },
  {
    id: 'plurals', script: 'plural', scope: 'repo', enforce: 'hard-zero',
    find: /^OK: no literal-'s' pluralization found\./m,
    summary: () => "0 literal-'s' concatenations",
    whenFailed: {
      find: /^FAIL: (\d+) file\(s\) still use the literal-'s'/m,
      summary: m => `${m[1]} file(s) use the literal-'s' plural hack`,
    },
  },
  {
    id: 'pseudolocale', script: 'pseudo', scope: 'repo', enforce: 'hard-zero',
    find: /^OK: en-XA\.json matches (\d+) English keys/m,
    summary: m => `en-XA matches en · ${m[1]} keys`,
  },
  {
    // DNT moved here from the render gate when `LOCALES` came down to `en-XA`
    // alone: the assertion cannot run under the pseudolocale (every term renders
    // accented by design), so leaving it there would have meant a gate that
    // renders, reports and exits 0 while checking no term. Comparing catalog
    // VALUES covers all 9 shipped locales instead of the one probe locale a render
    // budget could afford, and costs no render time. Hard-zero: a respelt product
    // name is a defect in every locale, with no ceiling to inherit.
    id: 'dnt', script: 'dnt', scope: 'repo', enforce: 'hard-zero',
    find: /^OK: (\d+) DNT term\(s\) intact across (\d+) catalog\(s\) — (\d+) value/m,
    summary: m => `${m[1]} terms intact · ${m[2]} catalogs · ${m[3]} values`,
    whenFailed: {
      find: /^\[dnt\] (\d+) do-not-translate violation\(s\) across (\d+) catalog/m,
      summary: m => `${m[1]} respelt term(s) across ${m[2]} catalog(s)`,
    },
  },
  {
    // Built-in app metadata is owned by `app.json` on the Python side, and
    // `APP_MANIFEST_KEY` localises it WITHOUT replacing the manifest's English --
    // so `kirocrew app list` keeps printing English by construction rather than by
    // a fallback. The price of that design is two copies of the same sentence, and
    // this is what stops them drifting: edit a `description` in `app.json` and,
    // unchecked, the CLI shows the new words while the dashboard shows the old ones
    // and nine translations silently describe a string that no longer exists.
    // Hard-zero and honestly so: both sides are files at this commit, no baseline is
    // read, and a failure names the exact key and both strings.
    id: 'manifest-sync', script: 'manifest', scope: 'repo', enforce: 'hard-zero',
    find: /^OK: (\d+) built-in manifests, (\d+) strings match/m,
    summary: m => `${m[1]} manifests · ${m[2]} strings in sync`,
    whenFailed: {
      find: /^\[app-manifest-sync\] FAIL — (\d+) problem\(s\)/m,
      summary: m => `${m[1]} manifest/catalog mismatch(es)`,
    },
  },
  {
    id: 'dynamic-keys', script: 'keys', scope: 'repo', enforce: 'info',
    find: /resolve, (\d+) dynamic site\(s\) at baseline \(([\d.]+)% static/m,
    summary: m => `${m[1]} unresolvable site(s) · ${m[2]}% static`,
    over: {
      find: /^\[dynamic-keys\] REPORT: (\d+) file\(s\) (gained call sites|improved)/m,
      summary: m => `${m[1]} file(s) ${m[2] === 'improved' ? 'improved' : 'gained unresolvable call sites'}`,
    },
  },
  {
    id: 'extractable', script: 'codemod', scope: 'repo', enforce: 'info',
    // Three mutually exclusive shapes below the ceiling, and they differ in prefix AND
    // separator: `N unextracted string(s) — below …` when under, and
    // `OK: N unextracted string(s), at …` when exactly AT it. An earlier version
    // anchored on the digit and so missed the equality line entirely — with the live
    // count at 2 and the ceiling at 3, ONE added string would have made the row MISSING
    // and failed the step on a healthy tree. The `over` shape is deliberately excluded
    // here (`unextracted user-visible string(s)`) so it resolves through `over` instead.
    find: /^(?:OK: )?(\d+) unextracted string\(s\)[ ,]/m,
    summary: m => `${m[1]} unextracted`,
    note: 'ceiling 3',
    over: {
      find: /^\[extractable\] REPORT: (\d+) unextracted user-visible string\(s\)/m,
      summary: m => `${m[1]} unextracted — over the ceiling`,
    },
  },
  {
    id: 'untranslated', script: 'strings', scope: 'repo', enforce: 'info',
    find: /^OK: (\d+) untranslated strings across (\d+) files, at or below the baseline of (\d+)/m,
    summary: m => `${m[1]} across ${m[2]} files`,
    note: m => `ceiling ${m[3]}, slack ${Number(m[3]) - Number(m[1])}`,
    over: {
      find: /^\[untranslated\] REPORT: (\d+) file\(s\) are over their committed ceiling:/m,
      summary: m => `${m[1]} file(s) over their per-file ceiling`,
    },
  },
  {
    // Added to `main` by #1099 while this branch was in flight, and the runner's first
    // encounter with it is instructive: the table did not model it, so the row went
    // "no check could say why" and the step FAILED CLOSED rather than passing. That is
    // the designed behaviour, but an unmodelled check is still a check nobody can read,
    // which is the whole point of the table.
    id: 'allcaps', script: 'strings', scope: 'repo', enforce: 'info',
    find: /^OK: (\d+) untranslated string\(s\) inside ALL-CAPS constants across (\d+) files, at or below the ceiling of (\d+)/m,
    summary: m => `${m[1]} inside ALL-CAPS constants across ${m[2]} files`,
    note: m => `ceiling ${m[3]}, slack ${Number(m[3]) - Number(m[1])}`,
    over: {
      find: /^\[allcaps\] REPORT: (\d+) untranslated string\(s\) sit inside ALL-CAPS module\n?constants, (\d+) above the ceiling of (\d+)/m,
      summary: m => `${m[1]} inside ALL-CAPS constants — ${m[2]} over the ceiling of ${m[3]}`,
    },
  },
]

export const ENFORCE_LABEL = {
  zero: 'zero tolerance',
  'hard-zero': 'hard zero',
  info: 'report only',
}

/** How many checks a given script owns — a single-check script needs no attribution. */
const ownedBy = (checks, key) => checks.filter(c => c.script === key).length

/**
 * Turn the captured output into one row per check.
 *
 * @param {object}  a
 * @param {object[]} a.checks
 * @param {Map<string, {status: number, text: string}>} a.runs
 * @param {string}  a.base    the base ref, '' when none was supplied
 */
export function resolveRows({ checks = CHECKS, runs, base = '' }) {
  return checks.map(c => {
    const run = runs.get(c.script)
    if (!run) return { ...c, state: 'MISSING', summary: 'its script did not run at all', note: '' }

    // `over` is tried FIRST, before the success pattern. An `info` check above its
    // ceiling prints a `[<id>] REPORT:` line and does NOT exit non-zero, so its script
    // may go on to print a success line too — `check-i18n-keys.mjs` must, because
    // `[key-refs]` reads that same line as ITS signal and suppressing it would make
    // that row MISSING. Matching the generic success line first would then hide the
    // growth: the row would read "at baseline" while the REPORT sat in the collapsed
    // group. The more specific pattern wins.
    const over = c.over && run.text.match(c.over.find)
    if (over) {
      return { ...c, state: 'PASS', summary: c.over.summary(over), note: 'over the record' }
    }

    const hit = run.text.match(c.find)
    if (hit) {
      const ok = c.ok ? c.ok(hit) : run.status === 0
      return {
        ...c,
        state: ok ? 'PASS' : 'FAIL',
        summary: c.summary(hit),
        note: typeof c.note === 'function' ? c.note(hit) : (c.note || ''),
      }
    }

    // Most scripts print a DIFFERENT sentence when they fail; try that before
    // concluding anything about a missing success line.
    const miss = c.whenFailed && run.text.match(c.whenFailed.find)
    if (miss) return { ...c, state: 'FAIL', summary: c.whenFailed.summary(miss), note: '' }

    // A diff-scoped check with no base did not run, and its script says so and exits 0.
    // Calling that MISSING made a healthy local run — where `I18N_BASE_REF` is normally
    // unset, and which `website/AGENTS.md` tells contributors to do — fail while
    // accusing four working checks of having "quietly stopped measuring".
    if (!base && c.scope === 'diff') {
      return { ...c, state: 'NOT RUN', summary: 'no base commit supplied', note: '' }
    }

    // The script failed. If it owns exactly one check, that check is the failure —
    // there is nothing else it could have been.
    if (run.status !== 0 && ownedBy(checks, c.script) === 1) {
      return { ...c, state: 'FAIL', summary: 'failed — see the group below', note: '' }
    }

    // The script failed and owns several checks, so this one's verdict is unknown:
    // within a script the checks are still sequential, and `check-i18n-keys.mjs` never
    // evaluates its ratchet once a dangling reference has failed it. Claiming a failure
    // here would send the author to fix something intact.
    if (run.status !== 0) {
      return { ...c, state: 'NOT RUN', summary: 'script exited before reaching this check', note: '' }
    }

    // Script exited 0 and printed nothing this row recognises: the genuine
    // stopped-printing case. Eleven rows must not silently become ten.
    return { ...c, state: 'MISSING', summary: 'no summary line and the script passed', note: '' }
  })
}

/**
 * Decide the run.
 *
 * **A non-zero child always fails, whatever the rows say.** That is the whole
 * invariant, and it is deliberately not expressed as "some row is FAIL": the first
 * version of this runner did exactly that and exited 0 while a script had crashed,
 * because every row it owned had been classified as an unknown rather than a failure.
 * Rows attribute; the child status decides.
 *
 * `unexplained` is then only a reporting concern — a script that failed with no row
 * able to name why, so the reader is pointed at its raw output instead of a row.
 */
export function verdict({ rows, runs, scripts = SCRIPTS }) {
  const bad = rows.filter(r => r.state === 'FAIL' || r.state === 'MISSING')
  const unknown = rows.filter(r => r.state === 'NOT RUN')
  const crashed = scripts.filter(s => (runs.get(s.key)?.status ?? 0) !== 0)
  const unexplained = crashed.filter(
    s => !rows.some(r => r.script === s.key && (r.state === 'FAIL' || r.state === 'MISSING')),
  )
  return {
    bad,
    unknown,
    unexplained,
    crashed,
    failed: bad.length > 0 || crashed.length > 0,
  }
}
