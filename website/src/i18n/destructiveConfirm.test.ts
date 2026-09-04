/**
 * Guards on destructive-confirmation copy across every shipped language.
 *
 * A mistranslated count badge is cosmetic. A mistranslated *confirmation* is
 * not: it either blocks a user from completing an action they intend, or
 * describes a destructive action inaccurately enough that they consent to
 * something they did not mean. Both are real failure modes, so both are
 * asserted here.
 */

import { describe, it, expect } from 'vitest'

import { OPERAND_QUOTE_PAIRS, DEFAULT_QUOTE_PAIR } from '../../scripts/lib/qa-checks.mjs'
import { CATALOGS } from './catalogs'
import { SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE } from './languages'
import { BULK_DELETE_TOKEN } from '../pages/SchedulePage'
import { BULK_PR_CLOSE_TOKEN, SEQUENTIAL_MERGE_TOKEN } from '../apps/issue-radar/components/PrBulkBar'

function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') Object.assign(out, flatten(value, path))
    else out[path] = String(value)
  }
  return out
}

const FLAT: Record<string, Record<string, string>> = Object.fromEntries(
  Object.entries(CATALOGS).map(([code, bundle]) => [
    code,
    flatten((bundle as { translation: unknown }).translation),
  ]),
)

/**
 * Authored catalogs only. The pseudolocale is a mechanical transform of English, so its
 * confirmation token is accented by construction and asserting on it would test the
 * generator, not the copy.
 */
const AUTHORED = SUPPORTED_LANGUAGES.filter(l => !l.devOnly)

const NON_DEFAULT = AUTHORED.filter(l => l.code !== DEFAULT_LANGUAGE)

describe('bulk-delete confirmation token', () => {
  it('is a code constant, never a catalog value', () => {
    // The token is compared verbatim against user input, so it must not be
    // reachable by a translator. If it ever became a catalog key, every
    // non-English user would be locked out of bulk delete.
    expect(BULK_DELETE_TOKEN).toBe('delete')
    for (const { code } of AUTHORED) {
      const offenders = Object.entries(FLAT[code])
        .filter(([k, v]) => k.startsWith('pages.schedulePage.') && v.trim() === BULK_DELETE_TOKEN)
        .map(([k]) => k)
      expect(offenders, `${code} exposes the safety token as copy: ${offenders.join(', ')}`)
        .toEqual([])
    }
  })

  it('the PR bulk-close token is a code constant, never a catalog value', () => {
    // Same rule, second call site: Issue Radar's bulk close gates on a typed
    // token. The hazard is identical — a translated token locks every non-English
    // user out of the action — but the guard has to name this token explicitly,
    // because the assertion above only scans the `pages.schedulePage.` prefix.
    expect(BULK_PR_CLOSE_TOKEN).toBe('close prs')
    // It must not collide with ANY label in the bar, in any language: a token equal
    // to the button the user just pressed can be satisfied by copying that button,
    // which is not the deliberate second act a confirmation is for.
    for (const { code } of AUTHORED) {
      const offenders = Object.entries(FLAT[code])
        .filter(([k, v]) =>
          k.startsWith('apps.issueRadar.components.prBulkBar.')
          && v.trim().toLowerCase() === BULK_PR_CLOSE_TOKEN)
        .map(([k]) => k)
      expect(offenders, `${code} exposes the PR close token as copy: ${offenders.join(', ')}`)
        .toEqual([])
    }
  })

  it('the PR sequential-merge token is a code constant, never a catalog value', () => {
    // Third call site, same rule. This token guards the one IRREVERSIBLE action in the
    // bar, so a translation that happened to equal it would be the worst version of
    // this bug: the confirmation could be satisfied by copying a visible label.
    expect(SEQUENTIAL_MERGE_TOKEN).toBe('merge prs')
    // And it must differ from the close token, or typing one would arm the other —
    // two irreversibly different actions behind one phrase.
    expect(SEQUENTIAL_MERGE_TOKEN).not.toBe(BULK_PR_CLOSE_TOKEN)
    for (const { code } of AUTHORED) {
      const offenders = Object.entries(FLAT[code])
        .filter(([k, v]) =>
          k.startsWith('apps.issueRadar.components.prBulkBar.')
          && v.trim().toLowerCase() === SEQUENTIAL_MERGE_TOKEN)
        .map(([k]) => k)
      expect(offenders, `${code} exposes the PR merge token as copy: ${offenders.join(', ')}`)
        .toEqual([])
    }
  })

  it('keeps the instruction verb separate from the "Type" column header', () => {
    // English "Type" is a noun in the table header and an imperative verb in
    // the confirmation. One shared key forced translators to pick one meaning,
    // and es/pt both picked the noun ("Tipo delete para confirmar"), which is
    // not an instruction. Two keys is the fix; this asserts they stay two.
    for (const { code } of AUTHORED) {
      expect(FLAT[code]['pages.schedulePage.type_verb_to_confirm'],
        `${code} is missing the verb form`).toBeTruthy()
      expect(FLAT[code]['pages.schedulePage.type'],
        `${code} is missing the column header`).toBeTruthy()
    }
  })

  it('does not reuse the column-header noun as the instruction verb', () => {
    // In English the two are legitimately the same word. In a language that
    // distinguishes them, an identical value means the noun leaked into the
    // instruction — the exact es/pt defect.
    const same = NON_DEFAULT.filter(({ code }) =>
      FLAT[code]['pages.schedulePage.type_verb_to_confirm']
        === FLAT[code]['pages.schedulePage.type'])
      .map(({ code }) => code)
    expect(same, `verb and noun forms are identical in: ${same.join(', ')} — `
      + 'the instruction likely reads as a noun').toEqual([])
  })
})

describe('destructive confirmations are translated', () => {
  /**
   * Keys whose copy authorizes irreversible loss. Left in English, a
   * non-English user is asked to approve deletion in a language they may not
   * read — the one place a missing translation is a safety issue rather than a
   * cosmetic one.
   */
  const DESTRUCTIVE = [
    'pages.schedulePage.this_permanently_removes_the_selected_job_one',
    'pages.schedulePage.this_permanently_removes_the_selected_job_other',
    'pages.schedulePage.and_their_run_history_this_action_cannot_be_undo',
    // Auto-Improvement's commit confirmation: it pushes to a real branch and a published
    // commit cannot be recalled, so an operator reading it in English they do not speak is
    // being asked to authorize an irreversible remote change they cannot evaluate.
    'autoImprovement.commitConfirm',
  ]

  for (const { code } of NON_DEFAULT) {
    it(`${code} translates them`, () => {
      const en = FLAT[DEFAULT_LANGUAGE]
      const untranslated = DESTRUCTIVE
        .filter(k => en[k] !== undefined && FLAT[code][k] === en[k])
      expect(untranslated, `${code} left destructive copy in English: ${untranslated.join(', ')}`)
        .toEqual([])
    })
  }
})

/**
 * Confirm keys whose interpolated operand MUST be quoted in every authored
 * catalog, in that locale's own convention (#4653, #4657/#4677, #4676, #4821).
 *
 * A destructive confirm that interpolates a user-supplied name bare lets an
 * ordinary-word name blend into the sentence: a pet named "Everything"
 * produced "Reset Everything?", indistinguishable from a sentence about
 * resetting everything.
 *
 * The pattern sweep below is the convention detector: every `confirm` key
 * whose English value interpolates a placeholder must land here, or in
 * `CONFIRM_OPERAND_KEY_EXEMPTIONS`, or have only placeholder-name-exempt
 * interpolations. This list is then the per-locale glyph pin. When adding a
 * destructive confirm that interpolates a user-supplied name, add it here.
 * NOTE: the sweep only sees keys matching `/confirm/i` — a confirm prompt
 * named `…_title` / `delete_x` / `make_x_live` is invisible to it and must
 * be pinned here by hand (#5725; several entries below are exactly that).
 */
export const QUOTED_OPERAND_CONFIRM_KEYS = [
  'apps.awsControl.console.delete_confirm', // filename operand, quoted per locale
  'apps.awsControl.console.folder_delete_confirm', // folder-name operand, quoted per locale #4821
  'apps.awsControl.console.library_remove_confirm', // artifact-name operand, quoted per locale #6987
  'apps.codeReviewSage.components.learningRail.confirm_delete', // quoted since #4653
  // Review Fix push: the git remote/branch operands are identifiers, quoted per
  // locale so a branch named like prose cannot read as the rest of the sentence.
  'apps.codeReviewSage.reviewFix.taskPanel.confirm_push_title',
  'apps.codeReviewSage.reviewFix.taskPanel.confirm_push_body',
  'apps.crewCompanion.gallery.deleteConfirm', // ASCII quotes → locale pair #4821
  'apps.mdNotebook.row.deleteTitle', // already quoted; pin + fr NNBSP fix #5725
  'apps.meetings.list.deleteConfirm', // already quoted; pin + fr/it glyph fix #4821
  'apps.mochi.gallery.delete_confirm', // ASCII quotes → locale pair #4821
  'apps.mochi.reset.title', // quoted by #4677
  'apps.mochi.reset.desc', // #4676
  'apps.papyrus.page.delete_paper_confirm', // #4676
  'apps.papyrus.workspace.co_author_conflict_discard_confirm', // #4676
  'apps.papyrus.workspace.delete_file_confirm', // quoted by #4677
  'autoImprovement.commitConfirm', // bare {{branch}} #4821
  // The code-execution grant's title AND body. #5725 quoted only the title, which left
  // the scope sentence one line under it reading as prose about every app (#6016).
  'components.appstore.trustAppModal.failed', // bare {{app}} #6016
  'components.appstore.trustAppModal.failed_generic', // bare {{app}} #6016
  'components.appstore.trustAppModal.intro', // bare {{app}} #6016
  'components.appstore.trustAppModal.on_cancel', // bare {{app}} #6016
  'components.appstore.trustAppModal.scope', // bare {{app}} #6016
  'components.appstore.trustAppModal.title', // bare {{app}} on the code-execution grant #5725
  'components.artifactFolderDeleteDialog.delete_folder', // already quoted; pin #5725
  'pages.artifactDeployPage.destroy_confirm',
  'pages.artifactDeployPage.recall_confirm',
  'pages.artifactDeployPage.remove_profile_confirm',
  'pages.artifactsPage.remove_artifact_confirm',
  'pages.chatSidebar.delete_folder_confirm',
  'pages.devFleetPage.keeps_name_the_live_target_and_discards_the_stag', // cancel-branch body, bare {{name}}/{{staged}} #5725
  'pages.devFleetPage.keeps_this_checkout_the_live_target_and_discards', // cancel-branch body, bare {{staged}} #5725
  'pages.devFleetPage.make_name_live', // ASCII quotes → locale pair #5725
  'pages.devFleetPage.rebase_name', // ASCII quotes → locale pair #5725
  'pages.devFleetPage.remove_name', // ASCII quotes → locale pair #5725
  'pages.overview.promptsTab.delete_confirm', // quoted in all catalogs at introduction (#4634)
  'pages.overview.skillsTab.delete_confirm',
  'pages.overview.skillsTab.dismiss_confirm',
  'pages.overview.steeringTab.delete_confirm',
  'pages.schedulePage.cronFolders.confirm_delete_folder',
  'pages.schedulePage.delete_named_job', // ASCII quotes → locale pair #5725
  'pages.settings.remoteCrewPanel.confirm_delete_of', // was fully bare #4821
  'pages.settings.securityPanel.trustedApps.revoke_confirm_title',
  'pages.settings.securityPanel.trustedApps.revoke_confirm_body',
]

/**
 * Placeholder names that cannot read as prose, so they do not need quoting.
 * Numerals, closed-set schedule fragments, version ids, and system error
 * text cannot parse as the rest of the sentence. A key whose EVERY
 * interpolation is in this set is exempt from both the quoted-operand pin
 * and the key list.
 */
export const EXEMPT_CONFIRM_PLACEHOLDER_NAMES = new Set([
  'count',
  'lines',
  'verb',
  'number',
  'version',
  'newVersion',
  'time',
  'unit',
  'when',
  'error',
  'resources',
  'bucket',
  'distribution',
  // Set once by the edition's composition root (i18next defaultVariables),
  // never user-supplied, so it cannot smuggle prose into a confirm sentence.
  'productName',
])

/**
 * Keys matching `/confirm/i` that interpolate a user-facing placeholder but
 * are intentionally left unquoted, with the reason a later author needs.
 * Kind-word exemptions (#4657): the object kind sits next to the operand
 * ("the template {{name}}", "crew {{name}}"), so the name cannot parse as
 * the rest of the sentence. Do not add a new key here just because quoting
 * it would be more catalog work — quote it, or change the English to a
 * kind-word form and record that decision.
 */
export const CONFIRM_OPERAND_KEY_EXEMPTIONS: Record<string, string> = {
  'apps.awsControl.console.library_remove_confirm_slug':
    'the {{folder}} operand is an S3 key prefix rendered inside a <folder> tag as a '
    + 'monospace <code> chip, so the tag already delimits it and glyph quotes would '
    + 'double-decorate a path whose own slashes are the delimiter a reader checks',
  'apps.mochi.approval.inline_ask':
    'the {{tool}} operand renders as a styled <code> chip via renderAroundTool, '
    + 'so glyph quotes would double-decorate it (#5725)',
  'pages.agentsPage.delete_the_template_named_confirm':
    'kind word "template" sits next to the operand (#4657)',
  'pages.kiroCrewAgentsPage.delete_crew_named_confirm':
    'kind word "crew" sits next to the operand (#4657)',
}

function placeholdersIn(value: string): string[] {
  return [...value.matchAll(/\{\{(\w+)\}\}/g)].map(match => match[1])
}

describe('destructive-confirm operands are quoted', () => {
  const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

  for (const { code } of AUTHORED) {
    it(`${code} wraps every non-exempt operand of every listed confirm`, () => {
      // The pair table lives in scripts/lib/qa-checks.mjs so the plain-Node
      // gates and this test can never disagree about a locale's glyphs.
      // One quoted placeholder is not enough: a pinned key that mixes
      // `{{name}}` with a second user-supplied operand would otherwise ship
      // the second one bare.
      const [open, close] = (OPERAND_QUOTE_PAIRS as Record<string, [string, string]>)[code]
        ?? DEFAULT_QUOTE_PAIR
      const offenders: string[] = []
      for (const key of QUOTED_OPERAND_CONFIRM_KEYS) {
        const value = FLAT[code][key]
        if (value === undefined) {
          offenders.push(key)
          continue
        }
        const bare = placeholdersIn(value).filter(name => {
          if (EXEMPT_CONFIRM_PLACEHOLDER_NAMES.has(name)) return false
          const quoted = new RegExp(
            `${escapeRe(open)}\\{\\{${escapeRe(name)}\\}\\}${escapeRe(close)}`,
          )
          return !quoted.test(value)
        })
        if (bare.length > 0) offenders.push(`${key} (${bare.join(', ')})`)
      }
      expect(offenders,
        `${code} interpolates a destructive-confirm operand bare (expected `
        + `${open}{{…}}${close}): ${offenders.join(', ')}`)
        .toEqual([])
    })
  }
})

describe('confirm-key interpolations are quoted or explicitly exempt (#4821)', () => {
  const english = FLAT[DEFAULT_LANGUAGE]
  const confirmKeys = Object.entries(english)
    .filter(([key, value]) => /confirm/i.test(key) && /\{\{\w+\}\}/.test(value))

  it('every listed pin and exemption still exists in English', () => {
    const missingQuoted = QUOTED_OPERAND_CONFIRM_KEYS.filter(k => english[k] === undefined)
    const missingExempt = Object.keys(CONFIRM_OPERAND_KEY_EXEMPTIONS)
      .filter(k => english[k] === undefined)
    expect(missingQuoted, `quoted-operand pin names a missing key: ${missingQuoted.join(', ')}`)
      .toEqual([])
    expect(missingExempt, `exemption list names a missing key: ${missingExempt.join(', ')}`)
      .toEqual([])
  })

  it('a key is never both pinned and exempt', () => {
    const overlap = QUOTED_OPERAND_CONFIRM_KEYS
      .filter(k => k in CONFIRM_OPERAND_KEY_EXEMPTIONS)
    expect(overlap, `quoted and exempt at once: ${overlap.join(', ')}`).toEqual([])
  })

  it('every confirm key that interpolates is pinned, name-exempt, or key-exempt', () => {
    // Taxonomy (both axes, because each covers a class the other cannot):
    //   - placeholder NAME `count`/`lines`/`verb` → cannot parse as prose
    //   - key exemption → kind-word forms whose operand is `{{name}}` but
    //     already disambiguated (#4657)
    //   - quoted-operand pin → user-supplied names that must be glyph-quoted
    // A new confirm key with `{{name}}` and no kind word fails this test
    // until it is quoted in every catalog and added to the pin. That is the
    // failure #4653/#4657/#4676 kept hitting by hand.
    const uncovered = confirmKeys.filter(([key, value]) => {
      const names = placeholdersIn(value)
      if (names.length > 0 && names.every(n => EXEMPT_CONFIRM_PLACEHOLDER_NAMES.has(n))) {
        return false
      }
      if (key in CONFIRM_OPERAND_KEY_EXEMPTIONS) return false
      if (QUOTED_OPERAND_CONFIRM_KEYS.includes(key)) return false
      return true
    }).map(([key]) => key)
    expect(uncovered,
      `confirm key interpolates a user-facing operand with no quote pin and no `
      + `exemption (add it to QUOTED_OPERAND_CONFIRM_KEYS and quote every catalog, `
      + `or record a reason in CONFIRM_OPERAND_KEY_EXEMPTIONS): ${uncovered.join(', ')}`)
      .toEqual([])
  })
})
