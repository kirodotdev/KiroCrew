/**
 * The `[OPTION-ACTIONS: close=label]` marker — grammar, non-collision, and the rules that
 * keep a spurious marker from doing anything.
 *
 * The action marker's whole safety story is structural, so it is asserted rather than
 * commented:
 *  - NON-COLLISION IN BOTH DIRECTIONS. `OPTION_MARKER_RE` must never read an action
 *    marker as content choices, and `OPTION_ACTION_MARKER_RE` must never read `[OPTIONS:]`
 *    as an action. Every other part of the design leans on this, and a one-directional
 *    check would pass while the other direction leaked.
 *  - THE ENUM IS CLOSED. An unknown action is dropped, not forwarded, so a marker from a
 *    newer agent than this dashboard degrades to nothing rather than to an unpredictable
 *    local effect.
 *  - PROSE THAT DISCUSSES THE SYNTAX IS UNCHANGED. The reason the action lives in its own
 *    field rather than in a magic label is that labels are model-emitted prose; that
 *    argument is only worth anything if writing ABOUT the marker is also inert.
 */
import { describe, it, expect } from 'vitest'
import { parseOptions } from '../app-sdk/protocol'
import { OPTION_MARKER_RE, OPTION_ACTION_MARKER_RE, stripPartialOptionMarker, matchActionMarkers, stripOptionMarkers } from '../app-sdk/protocol/optionMarker'

/** Both consts are `g`-flagged, so `.exec`/`.test` would leave `lastIndex` advanced and the
 *  NEXT reader would silently scan from the wrong offset. Clone per probe — the same rule
 *  `parseOptions` follows. */
const matches = (re: RegExp, s: string): RegExpMatchArray[] => [...s.matchAll(new RegExp(re))]
const bodyOf = (re: RegExp, s: string, group: number): string | undefined =>
  matches(re, s).at(-1)?.[group]

/** What the pipeline does mid-stream: strip the completed markers, then suppress a
 *  partially-typed one. Mirrors the order in the renderer (parseOptions FIRST). */
const streaming = (raw: string): string => stripPartialOptionMarker(parseOptions(raw).text)

describe('OPTION_ACTION_MARKER_RE grammar', () => {
  it('matches the canonical form and captures the raw entry list', () => {
    expect(bodyOf(OPTION_ACTION_MARKER_RE, '[OPTION-ACTIONS: close=Nothing else, close this tab]', 1))
      .toBe(' close=Nothing else, close this tab')
  })

  it('is case-insensitive on the head', () => {
    for (const head of ['[OPTION-ACTIONS:', '[option-actions:', '[Option-Actions:']) {
      expect(parseOptions(`${head} close=Shut it]`).action, head).toEqual({ action: 'close', label: 'Shut it' })
    }
  })

  it.each([
    ['ASCII', ']'],
    ['U+3011 】', '\u3011'],
    ['U+FF3D ］', '\uFF3D'],
    ['U+3015 〕', '\u3015'],
  ])('accepts the %s closer, like the content marker does', (_label, closer) => {
    // A model that substitutes a lookalike does so regardless of which head it just wrote,
    // and here a broken end anchor is worse than a lost button: the raw marker is what the
    // user reads, and on other channels what TTS reads aloud.
    const parsed = parseOptions(`Done.\n[OPTION-ACTIONS: close=Close tab${closer}`)
    expect(parsed.action).toEqual({ action: 'close', label: 'Close tab' })
    expect(parsed.text).toBe('Done.')
  })

  it('tolerates the stray markdown-link close some models append', () => {
    const parsed = parseOptions('[OPTION-ACTIONS: close=Close tab](OPTION-ACTIONS)')
    expect(parsed.action).toEqual({ action: 'close', label: 'Close tab' })
    // Stripped from the text too — otherwise the whole thing renders as a purple link.
    expect(parsed.text).toBe('')
  })

  it('does NOT match when real prose follows on the same line', () => {
    // `] (note)` with a gap, or any trailing words, must fail the end anchor so the prose
    // survives. This is the same deliberate decline the content marker makes.
    expect(matches(OPTION_ACTION_MARKER_RE, '[OPTION-ACTIONS: close=X] and then some prose')).toEqual([])
    expect(matches(OPTION_ACTION_MARKER_RE, '[OPTION-ACTIONS: close=X] (note)')).toEqual([])
  })

  it('takes the LAST marker when several are present', () => {
    const parsed = parseOptions('[OPTION-ACTIONS: close=First]\nmiddle\n[OPTION-ACTIONS: close=Second]')
    expect(parsed.action).toEqual({ action: 'close', label: 'Second' })
  })

  it('does not catastrophically backtrack, and carries no nested quantifier', () => {
    // The tempered body is what makes this linear. Pinning the SHAPE fails fast and cheaply;
    // a regression in it would otherwise present as a wedged worker rather than a failure.
    const src = OPTION_ACTION_MARKER_RE.source
    expect(src).toContain('(?:[^[\\n]|\\[(?!OPTION-ACTIONS:|OPTIONS?:))*')
    expect(src).not.toMatch(/\([^)]*[+*]\)[+*]/)
    expect(parseOptions('[OPTION-ACTIONS:'.repeat(20000)).action).toBeNull()
  })

  it('shares body and tail with the content marker, so the grammars cannot drift', () => {
    // Composed from the same constants. If someone re-spells one of them by hand, the two
    // sources stop agreeing on everything after the head and this fails.
    const tailOf = (re: RegExp) => re.source.replace(/^\\\[OPTION(?:\(S\)\?|-ACTIONS):/, '')
    expect(tailOf(OPTION_ACTION_MARKER_RE)).toBe(tailOf(OPTION_MARKER_RE))
    expect(tailOf(OPTION_MARKER_RE)).not.toBe(OPTION_MARKER_RE.source) // the strip actually fired
    expect(OPTION_ACTION_MARKER_RE.flags).toBe(OPTION_MARKER_RE.flags)
  })
})

describe('two markers on ONE line', () => {
  const MIXED = 'Pick one. [OPTIONS: A | B] [OPTION-ACTIONS: close=C]'

  it('recognises BOTH markers and leaks neither', () => {
    // The tail used to require end-of-line, so on a shared line only the TRAILING
    // marker could match: the leading `[OPTIONS: A | B]` was left unmatched, which
    // dropped its pills AND leaked its raw marker text into the rendered prose.
    // With a destructive `close` alongside it, the surviving affordance was the one
    // that deletes the tab.
    const { options, action, text } = parseOptions(MIXED)
    expect(options).toEqual(['A', 'B'])
    expect(action).toEqual({ action: 'close', label: 'C' })
    expect(text).not.toContain('[OPTIONS:')
    expect(text).not.toContain('[OPTION-ACTIONS:')
    expect(text).toBe('Pick one.')
  })

  it('recognises both when the ACTION marker comes first', () => {
    const { options, action, text } = parseOptions('Pick one. [OPTION-ACTIONS: close=C] [OPTIONS: A | B]')
    expect(options).toEqual(['A', 'B'])
    expect(action).toEqual({ action: 'close', label: 'C' })
    expect(text).toBe('Pick one.')
  })

  it('same-KIND pair: both are stripped and the LAST supplies the options', () => {
    // Previously the earlier of two same-kind markers could not match at all, so its
    // raw text leaked. Both are recognised now, and "the last marker wins" — already
    // the documented rule for markers on separate lines — decides the options.
    const { options, text } = parseOptions('Body [OPTIONS: A] [OPTIONS: B]')
    expect(options).toEqual(['B'])
    expect(text).toBe('Body')
  })

  it('still refuses a marker followed by ordinary prose on the same line', () => {
    // The terminator admits a sibling MARKER, not arbitrary trailing text. This shape
    // stays deliberately unparsed so a sentence mentioning the syntax renders as
    // written.
    const { options, text } = parseOptions('See [OPTIONS: A | B] for details')
    expect(options).toEqual([])
    expect(text).toBe('See [OPTIONS: A | B] for details')
  })
})

describe('an action marker NESTED inside an earlier UNCLOSED marker', () => {
  /**
   * The content pattern's body is tempered against every head, so it cannot cross into
   * a nested action marker — and with no closer before that head, the CONTENT marker
   * fails to match at all. The action pattern, though, scans INDEPENDENTLY, so it
   * happily matched the nested span and the row rendered a live destructive chip out of
   * text the reader sees as broken syntax. The label is model-emitted prose, so this is
   * reachable without any adversarial intent: one dropped `]` is enough.
   *
   * A malformed line must offer NOTHING. `close` tears the tab down, so "unparseable"
   * has to fail closed rather than fall back to the one affordance that deletes state.
   */
  const NESTED = '[OPTIONS: dropped closer [OPTION-ACTIONS: close=Delete everything]'

  it('offers no action, because the enclosing marker never closed', () => {
    expect(parseOptions(NESTED).action).toBeNull()
  })

  it('is rejected by the matcher itself, not by a downstream sanitiser', () => {
    // The refusal lives at the module's own scan, so a future consumer inherits it
    // instead of re-deriving it. Asserted alongside the fact that the RAW pattern still
    // matches: that is precisely why the helper has to exist and why scanning
    // `OPTION_ACTION_MARKER_RE` directly re-opens this hole. A pure-regex refusal would
    // need a variable-length lookbehind over the whole line.
    expect(matchActionMarkers(NESTED)).toEqual([])
    expect(matches(OPTION_ACTION_MARKER_RE, NESTED)).toHaveLength(1)
  })

  it('leaves the rejected span VISIBLE, because it is not a marker', () => {
    // A rejected span must not be stripped either: excising it would hide half a
    // malformed line while showing the other half.
    expect(parseOptions(NESTED).text).toBe(NESTED)
    expect(stripOptionMarkers(NESTED)).toBe(NESTED)
  })

  it('NEGATIVE CONTROL: the same action marker still parses once the enclosing marker closes', () => {
    // Fails for the intended reason — this differs from NESTED only by the `]` that
    // closes the content marker, so a guard that over-rejects breaks here.
    const closed = '[OPTIONS: dropped closer] [OPTION-ACTIONS: close=Delete everything]'
    expect(parseOptions(closed).action).toEqual({ action: 'close', label: 'Delete everything' })
    expect(parseOptions(closed).options).toEqual(['dropped closer'])
  })

  it('NEGATIVE CONTROL: an action marker on its own line is unaffected by an unclosed marker above', () => {
    // The heads are LINE forms, so an unclosed marker on a PRIOR line must not poison
    // a well-formed marker on this one.
    const nextLine = '[OPTIONS: dropped closer\n[OPTION-ACTIONS: close=Delete everything]'
    expect(parseOptions(nextLine).action).toEqual({ action: 'close', label: 'Delete everything' })
  })
})

describe('non-collision between the two markers — BOTH directions', () => {
  it('OPTION_MARKER_RE does not match an action marker', () => {
    // Structural: it needs `OPTIONS:` or `OPTION:` immediately after the `[`, and
    // `[OPTION-` cannot supply either.
    for (const s of [
      '[OPTION-ACTIONS: close=X]',
      '[option-actions: close=X]',
      'Done.\n[OPTION-ACTIONS: close=Nothing else, close this tab]',
    ]) {
      expect(matches(OPTION_MARKER_RE, s), s).toEqual([])
      expect(parseOptions(s).options, s).toEqual([])
    }
  })

  it('OPTION_ACTION_MARKER_RE does not match a content marker', () => {
    for (const s of ['[OPTIONS: a | b]', '[OPTION: a]', '[options: a | b]', '[OPTIONS:]']) {
      expect(matches(OPTION_ACTION_MARKER_RE, s), s).toEqual([])
      expect(parseOptions(s).action, s).toBeNull()
    }
  })

  it('keeps both bodies out of the other marker when they share a LINE', () => {
    // MEASURED on the backend before the heads were shared: a body that forbids only its
    // OWN head consumes the other one, so `[OPTION-ACTIONS: close=B]`'s raw text became a
    // content BUTTON LABEL. The temper now names both heads, so the content pattern cannot
    // cross the action head at all.
    //
    // CHANGED: this asserted the content marker did not match AT ALL on a shared line
    // (`toBeUndefined`), which was the tail requiring `$` — and that was the defect, not
    // the guarantee: an unmatched leading marker dropped its pills and leaked its raw
    // text. The tail now also terminates before a sibling marker, so each pattern matches
    // its OWN body. The property this test exists for is unchanged and still asserted:
    // neither body reaches into the other.
    const pair = '[OPTIONS: A] [OPTION-ACTIONS: close=B]'
    expect(bodyOf(OPTION_MARKER_RE, pair, 2)).toBe(' A')
    expect(bodyOf(OPTION_ACTION_MARKER_RE, pair, 1)).toBe(' close=B')
    // Nothing anywhere reports the raw action marker as a choice.
    expect(parseOptions(pair).options.some(o => o.includes('OPTION-ACTIONS'))).toBe(false)
  })

  it('parses BOTH when each owns its own line', () => {
    const parsed = parseOptions('Pick.\n[OPTIONS: Alpha | Beta]\n[OPTION-ACTIONS: close=Nothing else]')
    expect(parsed.options).toEqual(['Alpha', 'Beta'])
    expect(parsed.action).toEqual({ action: 'close', label: 'Nothing else' })
    expect(parsed.multi).toBe(true)
    expect(parsed.text).toBe('Pick.')
  })

  it('parses both regardless of which marker comes first', () => {
    const parsed = parseOptions('[OPTION-ACTIONS: close=Nothing else]\n[OPTIONS: Alpha | Beta]')
    expect(parsed.options).toEqual(['Alpha', 'Beta'])
    expect(parsed.action).toEqual({ action: 'close', label: 'Nothing else' })
  })
})

describe('entry parsing', () => {
  it('splits on `|`, tolerates whitespace, and yields the FIRST valid entry only', () => {
    // The SPLIT is still the point under test, and whitespace around an entry must
    // still not reach the label. What changed deliberately: the result is bounded at
    // ONE entry. Accumulating every entry required a same-kind dedupe downstream that
    // could never fire over a one-member enum, and the published app-kit protocol
    // documents `[OPTION-ACTIONS: close=<label>]` singular — so the extra entries were
    // generality no caller could reach and no contract promised.
    expect(parseOptions('[OPTION-ACTIONS: close=First   |   close=Second]').action).toEqual({ action: 'close', label: 'First' })
  })

  it('splits on the FIRST `=` only, so a label may contain `=`', () => {
    expect(parseOptions('[OPTION-ACTIONS: close=Set x=1 and y=2]').action).toEqual({ action: 'close', label: 'Set x=1 and y=2' })
  })

  it('keeps a label containing `,` intact — the separator is `|` ALONE', () => {
    // The canonical label is `close=Nothing else, close this tab`. The content parser falls
    // back to `,` when no `|` is present; doing that here would tear this label in half.
    expect(parseOptions('[OPTION-ACTIONS: close=Nothing else, close this tab]').action).toEqual({ action: 'close', label: 'Nothing else, close this tab' })
  })

  it('keeps a label containing a closer, because the marker ends at the LAST one', () => {
    expect(parseOptions('[OPTION-ACTIONS: close=Done ]]').action).toEqual({ action: 'close', label: 'Done ]' })
  })

  it('DROPS an unknown action rather than forwarding it', () => {
    // The enum is the dispatch allow-list. Forwarding an unknown verb would make a newer
    // agent's marker do something this dashboard cannot reason about.
    for (const body of ['reboot=Restart everything', 'exec=rm -rf /', 'CLOSE_ALL=Close all tabs']) {
      expect(parseOptions(`[OPTION-ACTIONS: ${body}]`).action, body).toBeNull()
    }
  })

  it('drops the unknown entries and keeps the known one in a mixed marker', () => {
    expect(parseOptions('[OPTION-ACTIONS: reboot=Nope | close=Yes | exec=Nope]').action).toEqual({ action: 'close', label: 'Yes' })
  })

  it('accepts the action name in any casing, consistent with the head', () => {
    expect(parseOptions('[OPTION-ACTIONS: Close=Shut it]').action).toEqual({ action: 'close', label: 'Shut it' })
    expect(parseOptions('[OPTION-ACTIONS:  CLOSE = Shut it ]').action).toEqual({ action: 'close', label: 'Shut it' })
  })

  it('DROPS an entry with an empty label', () => {
    expect(parseOptions('[OPTION-ACTIONS: close=]').action).toBeNull()
    expect(parseOptions('[OPTION-ACTIONS: close=   ]').action).toBeNull()
    expect(parseOptions('[OPTION-ACTIONS: close=Keep | close=]').action).toEqual({ action: 'close', label: 'Keep' })
  })

  it('DROPS an entry with no `=` at all', () => {
    // That is a content option written under the wrong head; it names no action.
    expect(parseOptions('[OPTION-ACTIONS: Just a label]').action).toBeNull()
    expect(parseOptions('[OPTION-ACTIONS: close]').action).toBeNull()
    expect(parseOptions('[OPTION-ACTIONS: Just a label | close=Real]').action).toEqual({ action: 'close', label: 'Real' })
  })

  it('yields no actions for an empty body', () => {
    expect(parseOptions('[OPTION-ACTIONS:]').action).toBeNull()
  })

  it('reports an empty array, never undefined, when there is no marker', () => {
    // Callers spread this into React state; `undefined` would surface as a crash at the
    // first `.length` rather than as an absent chip.
    expect(parseOptions('Just prose.').action).toBeNull()
    expect(parseOptions('[OPTIONS: a | b]').action).toBeNull()
  })
})

describe('stripping action markers from the displayed text', () => {
  it('removes the marker and the whitespace it sat behind', () => {
    expect(parseOptions('All done.\n\n[OPTION-ACTIONS: close=Nothing else]').text).toBe('All done.')
  })

  it('removes ALL action markers, not just the last', () => {
    // Same reason the content path strips all of them: a stray earlier marker must not leak
    // as raw syntax, even though only the last one supplies the actions.
    const parsed = parseOptions('a\n[OPTION-ACTIONS: close=One]\nb\n[OPTION-ACTIONS: close=Two]')
    expect(parsed.text).toBe('a\n\nb')
    expect(parsed.action).toEqual({ action: 'close', label: 'Two' })
  })

  it('removes an action marker whose entries were ALL dropped', () => {
    // Otherwise the safest input — an action this build does not implement — is the one that
    // leaks its raw syntax to the user.
    expect(parseOptions('Done.\n[OPTION-ACTIONS: reboot=Restart]').text).toBe('Done.')
    expect(parseOptions('Done.\n[OPTION-ACTIONS: reboot=Restart]').action).toBeNull()
  })

  it('removes both kinds of marker from one message', () => {
    const parsed = parseOptions('Pick.\n[OPTIONS: Alpha | Beta]\n[OPTION-ACTIONS: close=Nothing else]')
    expect(parsed.text).toBe('Pick.')
  })

  it('leaves marker-less content byte-identical, whitespace included', () => {
    // The long-standing contract for ordinary prose; adding the action scan must not start
    // trimming every message.
    expect(parseOptions('  padded  ').text).toBe('  padded  ')
  })
})

describe('prose that merely DISCUSSES the syntax is unchanged', () => {
  // This is the acceptance criterion behind putting the action in its own field: an agent
  // writing docs about the feature must not emit a live control.
  it.each([
    ['head named mid-sentence', 'The marker is [OPTION-ACTIONS: close=label] and it closes the tab.'],
    ['head with no body at all', 'Emit [OPTION-ACTIONS: followed by entries.'],
    ['bare head in a sentence', 'The [OPTION-ACTIONS head is distinct from [OPTIONS.'],
    ['inline code fragment', 'Write `[OPTION-ACTIONS: close=X]` on its own line.'],
    ['marker then trailing prose', '[OPTION-ACTIONS: close=X] — but only when the note landed.'],
  ])('%s', (_name, prose) => {
    const parsed = parseOptions(prose)
    expect(parsed.action).toBeNull()
    expect(parsed.options).toEqual([])
    expect(parsed.text).toBe(prose)
  })

  it('offers nothing for a label-shaped word that only LOOKS like the head', () => {
    for (const s of ['[OPTIONAL: a | b]', '[OPTION-ACTION: close=X]', '[OPTIONACTIONS: close=X]']) {
      expect(parseOptions(s).action, s).toBeNull()
    }
  })
})

describe('streaming a partial action marker', () => {
  it.each([
    ['bare bracket', 'Done.\n['],
    ['mid-head', 'Done.\n[OPTION-'],
    ['further into the head', 'Done.\n[OPTION-ACTI'],
    ['complete head, no body', 'Done.\n[OPTION-ACTIONS:'],
    ['body forming', 'Done.\n[OPTION-ACTIONS: close=Nothi'],
    ['second entry forming', 'Done.\n[OPTION-ACTIONS: close=One | clo'],
  ])('hides it while streaming — %s', (_name, raw) => {
    expect(streaming(raw)).toBe('Done.')
  })

  it('hides a lower-case partial head too', () => {
    expect(streaming('Done.\n[option-acti')).toBe('Done.')
  })

  it('renders the SAME text as written when NOT streaming', () => {
    // On a finished message an unterminated marker is real content — prose about the syntax,
    // or a truncated turn — so only the isStreaming gate may hide it.
    for (const raw of ['Done.\n[OPTION-ACTIONS: close=Nothi', 'Done.\n[OPTION-ACTI', 'Done.\n[OPTION-ACTIONS:']) {
      expect(parseOptions(raw).text, raw).toBe(raw)
    }
  })

  it('reveals the chip and drops the raw text once the marker completes', () => {
    const done = parseOptions('Done.\n[OPTION-ACTIONS: close=Nothing else]')
    expect(done.text).toBe('Done.')
    expect(done.action).toEqual({ action: 'close', label: 'Nothing else' })
    // And the streaming probe agrees, so there is no frame where both are visible.
    expect(streaming('Done.\n[OPTION-ACTIONS: close=Nothing else]')).toBe('Done.')
  })

  it('does not hold ordinary prose that happens to start with a bracket', () => {
    // The mid-head branch requires consistent casing and a whitespace boundary, so these
    // are released rather than held for the width of the longer head.
    expect(streaming('See [Optional')).toBe('See [Optional')
    expect(streaming('index arr[0')).toBe('index arr[0')
    expect(streaming('See [OPTION-X')).toBe('See [OPTION-X')
  })
})
