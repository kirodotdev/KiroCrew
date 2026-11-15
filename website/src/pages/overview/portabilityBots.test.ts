import { describe, expect, it } from 'vitest'

import { BOT_NOTICE_KEYS, BOT_REASON_KEYS, botImportLines, botRows, isWholeBot, orphanChats } from './portabilityBots'
import { i18nT } from '../../i18n/t'

const wholeRow = { bot: 'Ada', slug: 'ada', roster: 'added', binding: 'rebuilt', crewmate_file: 'written', dashboard: 'restored', reasons: [], notices: [] }

describe('the closed vocabulary', () => {
  // The backend's tuples in src/kiro_crew/portability_bot_types.py, copied here
  // so a reason or notice added on one side and not the other fails.
  const REASONS = [
    'name_taken', 'slug_taken', 'member_id_taken', 'store_taken', 'store_not_carried', 'store_not_owned',
    'workspace_taken', 'config_rejected', 'no_member_id', 'member_id_ambiguous', 'slug_collision',
    'binding_exists_unusable', 'archive_binding_disagrees', 'transcript_predates_import', 'write_failed',
    'unsafe_name', 'user_agent_named', 'foreign_owner', 'unreadable_record',
    'artifact_missing', 'artifact_rejected', 'digest_mismatch', 'held_by_other_bot',
    'another_package_of_this_bot',
  ]
  const NOTICES = ['starting_abilities_only', 'rules_not_carried', 'goals_not_carried']

  it('has a catalog string for every reason and notice', () => {
    expect(Object.keys(BOT_REASON_KEYS).sort()).toEqual([...REASONS].sort())
    expect(Object.keys(BOT_NOTICE_KEYS).sort()).toEqual([...NOTICES].sort())
    for (const key of [...Object.values(BOT_REASON_KEYS), ...Object.values(BOT_NOTICE_KEYS)]) {
      expect(i18nT(key, { name: 'x' })).not.toBe(key)
    }
  })
})

describe('botRows', () => {
  it('keeps named rows and drops values outside the vocabulary', () => {
    const rows = botRows([
      { ...wholeRow, reasons: ['write_failed', 'made_up', 'write_failed'], notices: ['rules_not_carried', '{{x}}'] },
      { slug: 'nameless' },
      'junk',
      null,
    ])
    expect(rows).toHaveLength(1)
    expect(rows[0].reasons).toEqual(['write_failed'])
    expect(rows[0].notices).toEqual(['rules_not_carried'])
    expect(botRows({ not: 'a list' })).toEqual([])
  })

  it('reads reasons given as a map of part to reason', () => {
    expect(botRows([{ ...wholeRow, reasons: { dashboard: 'digest_mismatch', binding: 'nope' } }])[0].reasons)
      .toEqual(['digest_mismatch'])
  })

  it('counts a Bot whole only when no part has a not-whole status', () => {
    expect(isWholeBot(botRows([wholeRow])[0])).toBe(true)
    expect(isWholeBot(botRows([{ ...wholeRow, binding: 'not_rebuilt' }])[0])).toBe(false)
    // An unknown status is reported, never taken as whole.
    expect(isWholeBot(botRows([{ ...wholeRow, dashboard: 'mystery' }])[0])).toBe(false)
  })
})

describe('botImportLines', () => {
  it('adds nothing for a whole Bot with no notice', () => {
    expect(botImportLines({ bots: [wholeRow] })).toEqual({ errors: [], warnings: [] })
    expect(botImportLines({})).toEqual({ errors: [], warnings: [] })
  })

  it('names a skipped Bot with its reasons', () => {
    const { errors } = botImportLines({ bots: [{ bot: 'Ada', slug: 'ada', roster: 'skipped', reasons: ['slug_taken', 'name_taken'] }] })
    expect(errors).toEqual(['Bot “Ada” was not added. Why: A Bot here already uses the same short name and A Bot with this name already exists here.'])
  })

  it('names a skipped Bot with no reason', () => {
    expect(botImportLines({ bots: [{ bot: 'Ada', roster: 'skipped' }] }).errors)
      .toEqual(['Bot “Ada” was not added.'])
  })

  it('lists each missing part of a Bot that was added', () => {
    const { errors, warnings } = botImportLines({ bots: [
      { ...wholeRow, binding: 'not_rebuilt', dashboard: 'page_not_rebuilt', reasons: ['archive_binding_disagrees'] },
      { ...wholeRow, bot: 'Bea', briefing: 'not_restored' },
    ] })
    expect(errors).toEqual([
      'Bot “Ada” was imported without everything it had. Missing: DM thread link and dashboard page. Why: The archive\'s DM thread link does not match this Bot.',
      'Bot “Bea” was imported without everything it had. Missing: briefing.',
    ])
    expect(warnings).toEqual([])
  })

  it('adds one warning per notice, without making the Bot not whole', () => {
    const { errors, warnings } = botImportLines({ bots: [
      { ...wholeRow, notices: ['starting_abilities_only', 'rules_not_carried', 'goals_not_carried'] },
    ] })
    expect(errors).toEqual([])
    expect(warnings).toEqual([
      'Bot “Ada” starts with its starting abilities only: abilities added on the other computer were not carried.',
      'The rules of Bot “Ada” were not carried. Set them again on this computer.',
      'The goals of Bot “Ada” were not carried. Set them again on this computer.',
    ])
  })

  it('lists the withheld Bot chats, with the Bots here that share their short name', () => {
    const { warnings } = botImportLines({
      bot_transcripts_orphaned: [
        { slug: 'cid', generation: 2, candidates: ['Cid'] },
        { slug: 'dot', generation: 1, candidates: [] },
        { generation: 1 },
      ],
      sessions_member_withheld: 4,
    })
    expect(warnings).toEqual([
      'Bot chats not imported because no imported Bot owns them: cid (same short name as Cid here), dot, and 2 more.',
    ])
  })

  it('gives the count alone when no orphan row is listed', () => {
    expect(botImportLines({ sessions_member_withheld: 3 }).warnings)
      .toEqual(['Bot chats not imported because no imported Bot owns them: 3.'])
  })

  it('shows the count lines', () => {
    const { warnings } = botImportLines({
      bots_more: 5,
      bot_dashboards_not_carried: true,
      member_records_not_carried_more: 7,
      crewmate_files_customised_more: 2,
    })
    expect(warnings).toEqual([
      'More Bot results not shown: 5.',
      'This archive comes from an earlier version and carries no Bot dashboards.',
      'More Bot entries whose rules or goals were not carried: 7.',
      'More Bot entries that start with their starting abilities only: 2.',
    ])
  })

  it('reads the briefing part, whole when it is none and missing when not restored', () => {
    expect(botImportLines({ bots: [{ ...wholeRow, briefing: 'none' }] })).toEqual({ errors: [], warnings: [] })
    expect(botImportLines({ bots: [{ ...wholeRow, briefing: 'not_restored', reasons: ['held_by_other_bot'] }] }).errors).toEqual([
      'Bot “Ada” was imported without everything it had. Missing: briefing. Why: It kept a different Bot\'s copy that was already on this computer.',
    ])
  })

  it('reports a Bot restore that failed as a whole, first, with its error class', () => {
    const { errors } = botImportLines({ bots_failed: 'OSError', bots: [{ ...wholeRow, binding: 'not_rebuilt' }] })
    expect(errors[0]).toBe('Bot data could not be restored: OSError.')
    expect(errors).toHaveLength(2)
  })

  it('reports a failed restore without detail when the value is not a class name', () => {
    expect(botImportLines({ bots_failed: 'boom <b>{{x}}</b>' }).errors).toEqual(['Bot data could not be restored.'])
    expect(botImportLines({ bots_failed: null }).errors).toEqual([])
  })

  it('ignores malformed counts', () => {
    expect(botImportLines({ bots_more: -1, sessions_member_withheld: '3', bot_dashboards_not_carried: 'yes' }).warnings).toEqual([])
  })

  it('shows a Bot name exactly as stored, never as a catalog key or template', () => {
    const name = '{{reasons}} $t(app.close)'
    expect(botImportLines({ bots: [{ bot: name, roster: 'skipped' }] }).errors)
      .toEqual([`Bot “${name}” was not added.`])
  })
})

describe('orphanChats', () => {
  it('drops rows without a slug and repeats among candidates', () => {
    expect(orphanChats([{ slug: 'a', candidates: ['X', 'X', 3] }, { slug: '' }, 7])).toEqual([{ slug: 'a', candidates: ['X'] }])
  })
})

describe('what the roster and agent-file lines say', () => {
  it('says "could not be set up" only for a Bot in the list that could not be set up', () => {
    const { errors } = botImportLines({ bots: [
      { bot: 'Ada', slug: 'ada', roster: 'skipped', reasons: ['store_not_owned'] },
      { bot: 'Bea', slug: 'bea', roster: 'skipped', reasons: ['unreadable_record'] },
      { bot: 'Cy', slug: 'cy', roster: 'skipped', reasons: ['workspace_taken'] },
    ] })
    expect(errors).toEqual([
      'Bot “Ada” could not be set up. Why: Its memory store belongs to another Bot.',
      'Bot “Bea” could not be set up. Why: A saved record could not be read.',
      'Bot “Cy” was not added. Why: Its workspace name is already used here.',
    ])
  })

  it('says a Bot runs on an agent file this import did not write when another Bot held it', () => {
    const held = { ...wholeRow, crewmate_file: 'not_written', reasons: ['held_by_other_bot'] }
    const { errors } = botImportLines({ bots: [held] })
    expect(errors).toEqual([
      'Bot “Ada” runs on an agent file this import did not write, which was already on this computer. Check what it allows before you use this Bot.',
    ])
    // Another missing part still gets its own line next to it.
    expect(botImportLines({ bots: [{ ...held, briefing: 'not_restored' }] }).errors).toEqual([
      'Bot “Ada” was imported without everything it had. Missing: briefing. Why: It kept a different Bot\'s copy that was already on this computer.',
      'Bot “Ada” runs on an agent file this import did not write, which was already on this computer. Check what it allows before you use this Bot.',
    ])
    // An agent file refused for its own reason is no other Bot's file.
    expect(botImportLines({ bots: [{ ...wholeRow, crewmate_file: 'not_written', reasons: ['user_agent_named'] }] }).errors)
      .toEqual(['Bot “Ada” was imported without everything it had. Missing: agent file. Why: One of your own agents already has this name.'])
  })
})

describe('the per-part reasons a current backend sends (part_reasons, in_roster)', () => {
  const parts = { roster: '', binding: '', crewmate_file: '', dashboard: '', briefing: '' }

  it('names a failed agent file as missing, never as another Bot\'s, when only the binding is held', () => {
    // The merged list holds held_by_other_bot (from the binding): it must not make
    // the agent-file line say the Bot runs on another Bot's file.
    const row = {
      ...wholeRow,
      crewmate_file: 'not_written',
      binding: 'not_rebuilt',
      reasons: ['write_failed', 'held_by_other_bot'],
      part_reasons: { ...parts, crewmate_file: 'write_failed', binding: 'held_by_other_bot' },
      in_roster: true,
    }
    expect(botImportLines({ bots: [row] }).errors).toEqual([
      'Bot “Ada” was imported without everything it had. Missing: DM thread link and agent file. Why: It kept a different Bot\'s copy that was already on this computer and A file could not be written.',
    ])
  })

  it('says a Bot runs on another Bot\'s agent file only when that file\'s own reason says so', () => {
    const row = {
      ...wholeRow,
      crewmate_file: 'not_written',
      briefing: 'not_restored',
      reasons: ['held_by_other_bot', 'write_failed'],
      part_reasons: { ...parts, crewmate_file: 'held_by_other_bot', briefing: 'write_failed' },
      in_roster: true,
    }
    expect(botImportLines({ bots: [row] }).errors).toEqual([
      'Bot “Ada” was imported without everything it had. Missing: briefing. Why: A file could not be written.',
      'Bot “Ada” runs on an agent file this import did not write, which was already on this computer. Check what it allows before you use this Bot.',
    ])
  })

  it('says a Bot runs on an agent file this import did not write when another data home owns it', () => {
    const heldLine = 'Bot “Ada” runs on an agent file this import did not write, which was already on this computer. Check what it allows before you use this Bot.'
    const older = { ...wholeRow, crewmate_file: 'not_written', reasons: ['foreign_owner'] }
    const row = { ...older, part_reasons: { ...parts, crewmate_file: 'foreign_owner' }, in_roster: true }
    expect(botImportLines({ bots: [row] }).errors).toEqual([heldLine])
    // An older row (no part_reasons): foreign_owner is only ever the agent file's.
    expect(botImportLines({ bots: [older] }).errors).toEqual([heldLine])
  })

  it('never says "runs on another agent file" for an older row whose agent file failed to write', () => {
    // No part_reasons: held_by_other_bot (the binding's) next to write_failed (the
    // agent file's) must not read as an agent file in place.
    const older = {
      ...wholeRow,
      crewmate_file: 'not_written',
      binding: 'not_rebuilt',
      reasons: ['held_by_other_bot', 'write_failed'],
    }
    const { errors } = botImportLines({ bots: [older] })
    expect(errors).toHaveLength(1)
    expect(errors[0]).not.toContain('runs on an agent file')
    expect(errors[0]).toContain('agent file')
  })

  it('decides "not added" or "could not be set up" from in_roster and the roster reason', () => {
    const skipped = (bot: string, reason: string, inRoster: boolean) => ({
      bot, slug: bot.toLowerCase(), roster: 'skipped', reasons: [reason],
      part_reasons: { ...parts, roster: reason }, in_roster: inRoster,
    })
    expect(botImportLines({ bots: [
      skipped('Ada', 'unreadable_record', false),
      skipped('Bea', 'unreadable_record', true),
      skipped('Cy', 'no_member_id', false),
    ] }).errors).toEqual([
      'Bot “Ada” could not be set up. Why: A saved record could not be read.',
      'Bot “Bea” could not be set up. Why: A saved record could not be read.',
      'Bot “Cy” was not added. Why: It comes from an older version and has no Bot ID.',
    ])
  })

  it('never says "not added" when in_roster is false only because the Bot list could not be read', () => {
    // The backend sends in_roster false for every row when it cannot read the installed
    // Bot list; a reason that does not mean "not added" must not be read as one.
    const unknown = (bot: string) => ({
      bot, slug: bot.toLowerCase(), roster: 'skipped', reasons: ['unreadable_record'],
      part_reasons: { ...parts, roster: 'unreadable_record' }, in_roster: false,
    })
    const { errors } = botImportLines({ bots: [unknown('Ada'), unknown('Bea')] })
    expect(errors).toEqual([
      'Bot “Ada” could not be set up. Why: A saved record could not be read.',
      'Bot “Bea” could not be set up. Why: A saved record could not be read.',
    ])
    expect(errors.join(' ')).not.toContain('was not added')
  })

  it('never says "not added" for a Bot in the Bot list whose roster part gives no reason', () => {
    const inList = (bot: string, inRoster: boolean) => ({
      bot, slug: bot.toLowerCase(), roster: 'skipped', reasons: [], part_reasons: { ...parts }, in_roster: inRoster,
    })
    expect(botImportLines({ bots: [inList('Ada', true), inList('Bea', false)] }).errors).toEqual([
      'Bot “Ada” could not be set up.',
      'Bot “Bea” was not added.',
    ])
  })

  it('reads an older backend\'s row (no part_reasons, no in_roster) as before', () => {
    const rows = botRows([{ ...wholeRow, crewmate_file: 'not_written', reasons: ['held_by_other_bot'] }])
    expect(rows[0].partReasons).toBeNull()
    expect(rows[0].inRoster).toBeNull()
    expect(botImportLines({ bots: [{ bot: 'Cy', roster: 'skipped', reasons: ['no_member_id'] }] }).errors)
      .toEqual(['Bot “Cy” was not added. Why: It comes from an older version and has no Bot ID.'])
  })

  it('drops part reasons outside the vocabulary', () => {
    const [row] = botRows([{ ...wholeRow, part_reasons: { binding: 'made_up', dashboard: 'digest_mismatch', other: 'write_failed' } }])
    expect(row.partReasons).toEqual({ dashboard: 'digest_mismatch' })
  })
})
