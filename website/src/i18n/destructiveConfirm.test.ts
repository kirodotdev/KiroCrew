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

  it('keeps the instruction sentence separate from the "Type" column header', () => {
    // English "Type" is a noun in the table header and an imperative verb in
    // the confirmation. One shared key forced translators to pick one meaning,
    // and es/pt both picked the noun ("Tipo delete para confirmar"), which is
    // not an instruction. The instruction is its own whole-sentence key.
    for (const { code } of AUTHORED) {
      expect(FLAT[code]['pages.schedulePage.type_token_to_confirm'],
        `${code} is missing the instruction`).toBeTruthy()
      expect(FLAT[code]['pages.schedulePage.type'],
        `${code} is missing the column header`).toBeTruthy()
    }
  })

  it('keeps exactly one <token/> slot in the instruction, in every language', () => {
    // The token renders through the slot, so a value that drops it hides the
    // word the user must type, and a doubled one shows it twice.
    for (const { code } of AUTHORED) {
      const v = FLAT[code]['pages.schedulePage.type_token_to_confirm']
      expect(v.split('<token/>').length - 1, `${code}: ${v}`).toBe(1)
    }
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
    'pages.schedulePage.bulk_delete_removes_jobs_and_history_one',
    'pages.schedulePage.bulk_delete_removes_jobs_and_history_other',
    'pages.schedulePage.delete_n_scheduled_jobs_title_one',
    'pages.schedulePage.delete_n_scheduled_jobs_title_other',
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
 * Confirm prompts whose interpolated operand MUST be quoted in every authored
 * catalog, in that locale's own convention.
 *
 * A destructive confirm that interpolates a user-supplied name bare lets an
 * ordinary-word name blend into the sentence: a pet named "Everything"
 * produced "Reset Everything?", indistinguishable from a sentence about
 * resetting everything.
 *
 * Every key matching `/confirm/i` whose English value interpolates a
 * placeholder is swept in automatically (`quotedOperandKeys` below) unless it
 * interpolates only `EXEMPT_CONFIRM_PLACEHOLDER_NAMES` or carries a reason in
 * `CONFIRM_OPERAND_KEY_EXEMPTIONS`. This list holds only the confirm prompts the
 * sweep cannot see, named `…_title` / `delete_x` / `make_x_live`; a key the
 * sweep already reads does not belong here.
 */
export const QUOTED_OPERAND_CONFIRM_KEYS = [
  'apps.mdNotebook.row.deleteTitle', // already quoted; pin + fr NNBSP fix #5725
  'apps.mochi.reset.title', // quoted by #4677
  'apps.mochi.reset.desc', // #4676
  // The code-execution grant's title AND body. #5725 quoted only the title, which left
  // the scope sentence one line under it reading as prose about every app (#6016).
  'components.appstore.trustAppModal.failed', // bare {{app}} #6016
  'components.appstore.trustAppModal.failed_generic', // bare {{app}} #6016
  'components.appstore.trustAppModal.intro', // bare {{app}} #6016
  'components.appstore.trustAppModal.on_cancel', // bare {{app}} #6016
  'components.appstore.trustAppModal.scope', // bare {{app}} #6016
  'components.appstore.trustAppModal.title', // bare {{app}} on the code-execution grant #5725
  'pages.appsPage.uninstall_app_title', // app-name operand of the Library and App detail uninstall confirmations, quoted per locale
  'components.artifactFolderDeleteDialog.delete_folder', // already quoted; pin #5725
  'pages.devFleetPage.keeps_name_the_live_target_and_discards_the_stag', // cancel-branch body, bare {{name}}/{{staged}} #5725
  'pages.devFleetPage.keeps_this_checkout_the_live_target_and_discards', // cancel-branch body, bare {{staged}} #5725
  'pages.devFleetPage.make_name_live', // ASCII quotes → locale pair #5725
  'pages.devFleetPage.rebase_name', // ASCII quotes → locale pair #5725
  'pages.devFleetPage.remove_name', // ASCII quotes → locale pair #5725
  'pages.schedulePage.delete_named_job', // ASCII quotes → locale pair #5725
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
  'hooks.useSessionActions.close_confirm.reason_other_starter':
    'the {{name}} operand is the display name of the person who started the chat, the '
    + 'subject at the head of its own sentence ("{{name}} started this chat."), in a '
    + 'reason line whose confirm names no object: the chat being closed is never interpolated',
  'settings.advanced.reset_confirm':
    'the {{value}} operand is the setting\'s current value, printed after its own '
    + '"Current:" label at the end of the line, so the label already marks where it starts '
    + 'and nothing follows it to blur where it ends',
  'pages.membersPage.dashboard_download_confirm_body':
    'kind-word form: the {{name}} operand is the bot\'s display name, joined to its kind '
    + 'word ("{{name}}\'s Dashboard"), in a confirm for a download, which removes nothing',
  'components.redaction.link_open_confirm_title':
    'the {{host}} operand is a hostname the blocked-link record already passed through a '
    + 'strict host shape (letters, digits, dots, hyphens, underscores, or a bracketed IPv6 '
    + 'literal), so it cannot carry a space or a quote that would blur where it ends',
  'components.redaction.link_allow_confirm_title':
    'the {{host}} operand is a hostname validated by the same strict host shape as '
    + 'link_open_confirm_title, so it cannot carry a space or a quote',
  'components.redaction.link_open_confirm_body':
    'the {{chars}} operand is a non-negative integer character count, never user text',
  'pages.chatSidebar.resume_all_confirm_body':
    'the {{limit}} operand is RESUME_ALL_MAX_RUNNING, a positive integer constant in code, '
    + 'never user text',
  'components.awsConsentGate.confirmed_on':
    'not a confirmation prompt: a past-tense receipt fragment whose only operand is a '
    + 'machine-formatted date from fmtDate, never user-supplied text',
  'pages.membersPage.create_unconfirmed':
    'not a confirmation prompt: a non-interactive outcome notice stating whether the named '
    + 'mate was created; it offers no destructive action and the name identifies the '
    + 'uncertain result that the user must check in the roster',
  'pages.schedulePage.late_save_unconfirmed':
    'not a confirmation prompt: a non-interactive notice that a save sent from the closed '
    + 'job dialog was never confirmed; it offers no destructive action',
  'apps.awsControl.console.library_remove_confirm_slug':
    'the {{folder}} operand is an S3 key prefix rendered inside a <folder> tag as a '
    + 'monospace <code> chip, so the tag already delimits it and glyph quotes would '
    + 'double-decorate a path whose own slashes are the delimiter a reader checks',
  'apps.mochi.approval.inline_ask':
    'the {{tool}} operand renders as a styled <code> chip via renderAroundTool, '
    + 'so glyph quotes would double-decorate it (#5725)',
  'pages.kiroCrewAgentsPage.delete_crew_named_confirm':
    'kind word "crew" sits next to the operand (#4657)',
  'apps.awsControl.console.backup_restore_foreign_confirm':
    'the {{install}} operand is the first 8 hex characters of an install id this app '
    + 'mints itself (uuid4, never user-supplied text), and it already sits inside '
    + 'parentheses after the words "another install" -- so the risk glyph quotes exist '
    + 'to close, a crafted operand blending into the sentence, cannot arise here (#9554)',
  'pages.overview.workflowLibrary.remove_confirm_name':
    'the {{slug}} operand is the ref /workflow reaches the workflow by, its slash-command '
    + 'slug or else its id, both written by the workflow library itself (lowercase letters, '
    + 'digits, hyphens and underscores, no space or quote), and it sits directly after the literal '
    + '\"/workflow\" command word, so the command shape delimits it and glyph quotes would '
    + 'double-decorate a command whose own slash is the delimiter a reader checks (#18998)',
  'pages.settings.connectionsPanel.remove_confirm':
    'the {{provider}} operand is a Connections registry display name (GitHub, Asana), '
    + 'a fixed vendor brand never typed by a user, and the kind words "OAuth app" sit '
    + 'next to it -- a brand name in glyph quotes would read as a user-supplied label',
}

function placeholdersIn(value: string): string[] {
  return [...value.matchAll(/\{\{(\w+)\}\}/g)].map(match => match[1])
}

/** Keys matching `/confirm/i` whose English value interpolates a placeholder. */
function sweptConfirmKeys(): string[] {
  return Object.entries(FLAT[DEFAULT_LANGUAGE])
    .filter(([key, value]) => /confirm/i.test(key) && /\{\{\w+\}\}/.test(value))
    .map(([key]) => key)
}

/** Every confirm whose operand must be quoted: the sweep's non-exempt keys plus the hand pin. */
function quotedOperandKeys(): string[] {
  const english = FLAT[DEFAULT_LANGUAGE]
  const swept = sweptConfirmKeys().filter(key => {
    if (key in CONFIRM_OPERAND_KEY_EXEMPTIONS) return false
    return !placeholdersIn(english[key]).every(n => EXEMPT_CONFIRM_PLACEHOLDER_NAMES.has(n))
  })
  return [...new Set([...swept, ...QUOTED_OPERAND_CONFIRM_KEYS])]
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
      for (const key of quotedOperandKeys()) {
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

describe('confirm-key interpolations are quoted or explicitly exempt', () => {
  const english = FLAT[DEFAULT_LANGUAGE]

  it('sweeps the English catalog for confirm keys', () => {
    expect(sweptConfirmKeys().length, 'no /confirm/i key with a placeholder in English').toBeGreaterThan(0)
    expect(quotedOperandKeys().length).toBeGreaterThan(QUOTED_OPERAND_CONFIRM_KEYS.length)
  })

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

  it('the hand pin holds only confirm prompts the sweep cannot see', () => {
    const redundant = QUOTED_OPERAND_CONFIRM_KEYS.filter(k => /confirm/i.test(k))
    expect(redundant, `the /confirm/i sweep already quotes-checks these: ${redundant.join(', ')}`)
      .toEqual([])
  })
})
