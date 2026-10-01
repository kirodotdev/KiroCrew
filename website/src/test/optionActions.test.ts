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
 *  - PROSE THAT DISCUSSES THE SYNTAX IS UNCHANGED. A label is model-emitted prose, so
 *    the grammar's refusals are only worth anything if writing ABOUT the marker is inert.
 */
import { describe, it, expect } from 'vitest'
import type { ChatMessage } from '../types'
import { parseOptions } from '../app-sdk/protocol'
import { searchableText } from '../utils/searchableText'
import { OPTION_MARKER_PATTERN_SOURCE, OPTION_ACTION_MARKER_PATTERN_SOURCE, MARKER_PATTERN_FLAGS, stripPartialOptionMarker, matchActionMarkers, stripActionMarkers, stripOptionMarkers, labelsHaveUnmatchedOpener } from '../app-sdk/protocol/optionMarker'

// Rebuilt from the exported SOURCE strings rather than imported as regexes: the module
// exports no RegExp by design (its boundary test asserts that structurally), because a
// shared g-flagged instance hands out mutable `lastIndex` state. A local instance built
// here is this file's own, so probing it cannot disturb any production scan.
const OPTION_MARKER_RE = new RegExp(OPTION_MARKER_PATTERN_SOURCE, MARKER_PATTERN_FLAGS)
const OPTION_ACTION_MARKER_RE = new RegExp(OPTION_ACTION_MARKER_PATTERN_SOURCE, MARKER_PATTERN_FLAGS)

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
      expect(matchActionMarkers(`${head} close=Shut it]`).at(-1)?.[1], head).toBe(' close=Shut it')
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
    const raw = `Done.\n[OPTION-ACTIONS: close=Close tab${closer}`
    expect(matchActionMarkers(raw).at(-1)?.[1]).toBe(' close=Close tab')
    expect(parseOptions(raw).text).toBe('Done.')
  })

  it('tolerates the stray markdown-link close some models append', () => {
    const raw = '[OPTION-ACTIONS: close=Close tab](OPTION-ACTIONS)'
    expect(matchActionMarkers(raw).at(-1)?.[1]).toBe(' close=Close tab')
    // Stripped from the text of any row carrying prose of its own — otherwise the whole
    // thing renders as a purple link.
    expect(parseOptions(`Done.\n${raw}`).text).toBe('Done.')
    // When the marker is ALL the row has, it stays instead. The link form makes the trade
    // plainest: a dead link the user can read and search beats the blank, unfindable row
    // stripping left behind, because nothing renders the action until the chip layer lands.
    expect(parseOptions(raw).text).toBe(raw)
  })

  it('does NOT match when real prose follows on the same line', () => {
    // `] (note)` with a gap, or any trailing words, must fail the end anchor so the prose
    // survives. This is the same deliberate decline the content marker makes.
    expect(matches(OPTION_ACTION_MARKER_RE, '[OPTION-ACTIONS: close=X] and then some prose')).toEqual([])
    expect(matches(OPTION_ACTION_MARKER_RE, '[OPTION-ACTIONS: close=X] (note)')).toEqual([])
  })

  it('takes the LAST marker when several are present', () => {
    const raw = '[OPTION-ACTIONS: close=First]\nmiddle\n[OPTION-ACTIONS: close=Second]'
    expect(matchActionMarkers(raw).at(-1)?.[1]).toBe(' close=Second')
  })

  it('ends the marker at the LAST closer, so a label may contain one', () => {
    expect(matchActionMarkers('[OPTION-ACTIONS: close=Done ]]').at(-1)?.[1]).toBe(' close=Done ]')
  })

  it('does not catastrophically backtrack, and carries no nested quantifier', () => {
    // The tempered body is what makes this linear. Pinning the SHAPE fails fast and cheaply;
    // a regression in it would otherwise present as a wedged worker rather than a failure.
    const src = OPTION_ACTION_MARKER_RE.source
    // An ordinary body character excludes every bracket of either polarity — all four
    // openers and all four closers — so the body stops at the first plausible closer
    // instead of running to the last on the line, and an opener is never also an ordinary
    // character. A closer reaches the body only through the two conditional arms below.
    const OPENERS = ['\\[', '\\u3010', '\\uFF3B', '\\u3014']
    const CLOSERS = ['\\]', '\\u3011', '\\uFF3D', '\\u3015']
    expect(src).toContain(`[^${OPENERS.join('')}${CLOSERS.join('')}\\n]`)
    // A `[` inside the body never opens a fresh marker of either family.
    expect(src).toContain('\\[(?!OPTION-ACTIONS:|OPTIONS?:)')
    // MATCHED PAIR: the closer's `[` is matched inside the body, and the closer is NOT
    // followed by a separator or another closer. That exclusion is what keeps this arm
    // disjoint from the continuation arm, so no span has two parses and the body stays linear.
    expect(src).toContain('(?![ \\t]*[|,]|[\\]\\u3011\\uFF3D\\u3015])')
    // LIST CONTINUES: a closer that a separator or another closer follows.
    expect(src).toContain('[\\]\\u3011\\uFF3D\\u3015](?=[ \\t]*[|,]|[\\]\\u3011\\uFF3D\\u3015])')
    expect(src).not.toMatch(/\([^)]*[+*]\)[+*]/)
    expect(matchActionMarkers('[OPTION-ACTIONS:'.repeat(20000))).toEqual([])
  })

  it('shares body and tail with the content marker, so the grammars cannot drift', () => {
    // Composed from the same constants. If someone re-spells one of them by hand, the two
    // sources stop agreeing on everything after the head and this fails.
    const tailOf = (re: RegExp) => re.source.replace(/^\\\[OPTION(?:\(S\)\?|-ACTIONS):/, '')
    // The content pattern is a two-branch alternation now (a wrapper form and a plain
    // one), so the shared body+tail is a SUBSTRING of its source rather than all of it.
    expect(OPTION_MARKER_RE.source).toContain(tailOf(OPTION_ACTION_MARKER_RE))
    expect(tailOf(OPTION_ACTION_MARKER_RE)).not.toBe(OPTION_ACTION_MARKER_RE.source)
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
    const { options, text } = parseOptions(MIXED)
    expect(options).toEqual(['A', 'B'])
    expect(matchActionMarkers(MIXED).at(-1)?.[1]).toBe(' close=C')
    expect(text).not.toContain('[OPTIONS:')
    expect(text).not.toContain('[OPTION-ACTIONS:')
    expect(text).toBe('Pick one.')
  })

  it('recognises both when the ACTION marker comes first', () => {
    const raw = 'Pick one. [OPTION-ACTIONS: close=C] [OPTIONS: A | B]'
    const { options, text } = parseOptions(raw)
    expect(options).toEqual(['A', 'B'])
    expect(matchActionMarkers(raw).at(-1)?.[1]).toBe(' close=C')
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
    expect(text).toEqual('See [OPTIONS: A | B] for details')
  })

  it.each([
    ['two content markers', '[OPTIONS: A] [OPTIONS: B] in docs'],
    ['content then action', '[OPTIONS: A] [OPTION-ACTIONS: close=B] in docs'],
    ['action then content', '[OPTION-ACTIONS: close=A] [OPTIONS: B] in docs'],
    ['three markers', '[OPTIONS: A] [OPTIONS: B] [OPTIONS: C] see above'],
  ])('a sibling CHAIN that does not reach the line end parses NOTHING — %s', (_name, raw) => {
    // The sibling terminator is all-or-nothing, and this is the shape that made it so.
    // Admitting a bare sibling HEAD let the first marker parse (a head follows) while the
    // last declined (prose follows), so the first marker's text was deleted from the
    // message and returned as a chip nobody wrote — silent prose deletion, which is the
    // one failure direction this grammar refuses everywhere else.
    const { options, text } = parseOptions(raw)
    expect(options, raw).toEqual([])
    expect(matchActionMarkers(raw), raw).toEqual([])
    expect(text, raw).toEqual(raw)
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
    expect(matchActionMarkers(closed).at(-1)?.[1]).toBe(' close=Delete everything')
    expect(parseOptions(closed).options).toEqual(['dropped closer'])
  })

  it('NEGATIVE CONTROL: an action marker on its own line is unaffected by an unclosed marker above', () => {
    // The heads are LINE forms, so an unclosed marker on a PRIOR line must not poison
    // a well-formed marker on this one.
    const nextLine = '[OPTIONS: dropped closer\n[OPTION-ACTIONS: close=Delete everything]'
    expect(matchActionMarkers(nextLine).at(-1)?.[1]).toBe(' close=Delete everything')
  })
})

describe('an action label whose brackets do NOT balance', () => {
  /**
   * The content path has refused this shape since #9284 (`labelsHaveUnmatchedOpener`): a
   * label carrying an unmatched `[` is indistinguishable from a marker the model never
   * closed, so the body runs through the following prose to reach a closer belonging to
   * something else — `arr[0]`'s — and removing the span deletes the sentence.
   *
   * The action path enforced only the NESTING check, which measures depth AT the marker's
   * offset and therefore cannot see a stray opener inside the body. So the identical shape
   * that renders as visible text under `[OPTIONS:` was accepted under `[OPTION-ACTIONS:`:
   * measured on the pre-fix tree, `[OPTION-ACTIONS: close=Close after checking arr[0]`
   * rendered as the empty string and offered a live `close` chip labelled
   * `Close after checking arr[0`. Failing closed matters more here than on the content
   * path — the affordance that survives this one tears the tab down.
   */
  const OVERREACH = [
    '[OPTION-ACTIONS: close=Close after checking arr[0]',
    'Done. [OPTION-ACTIONS: close=X. Also see arr[0]',
    '[OPTION-ACTIONS: close=Fix [x logging]',
    'Ready [OPTION-ACTIONS: close=Ship | reboot=Hold before you diff src/app[0]',
  ]

  it('and therefore deletes no prose', () => {
    for (const text of OVERREACH) {
      expect(parseOptions(text).text, text).toBe(text)
      expect(stripOptionMarkers(text), text).toBe(text)
    }
  })

  it('is refused by the module scan, not by a downstream sanitiser', () => {
    // Asserted alongside the RAW pattern still matching: that is why the refusal has to
    // live in the helper, and why a consumer scanning the pattern directly re-opens it.
    for (const text of OVERREACH) {
      expect(matchActionMarkers(text), text).toEqual([])
      expect(matches(OPTION_ACTION_MARKER_RE, text).length, text).toBeGreaterThan(0)
    }
  })

  it('refuses on the SAME rule the content path uses, not a second spelling of it', () => {
    for (const text of OVERREACH) {
      const body = matches(OPTION_ACTION_MARKER_RE, text).at(-1)?.[1] ?? ''
      expect(labelsHaveUnmatchedOpener(body), text).toBe(true)
    }
  })

  it('keeps what is OFFERED and what is HIDDEN in agreement', () => {
    // Both verdicts, so the balanced row is load-bearing rather than decorative: pinning
    // only the refused rows would pass on a stripper that hides everything.
    for (const text of [...OVERREACH, '[OPTION-ACTIONS: close=Fix [x] logging]']) {
      const offered = matchActionMarkers(text).length > 0
      expect(stripActionMarkers(text) !== text, text).toBe(offered)
    }
  })

  it('NEGATIVE CONTROL: a MATCHED pair mid-label still parses', () => {
    // Differs from the third OVERREACH row only by the `]` that closes `[x`, so a guard
    // that over-rejects fails here. Mirrors the content path's pinned `Fix [x] logging`.
    expect(matchActionMarkers('[OPTION-ACTIONS: close=Fix [x] logging]').at(-1)?.[1])
      .toBe(' close=Fix [x] logging')
  })

  it('NEGATIVE CONTROL: every lookalike pair counts as matched', () => {
    for (const [open, close] of [['[', ']'], ['【', '】'], ['［', '］'], ['〔', '〕']]) {
      const text = `[OPTION-ACTIONS: close=Fix ${open}x${close} logging]`
      expect(matchActionMarkers(text).at(-1)?.[1], text).toBe(` close=Fix ${open}x${close} logging`)
    }
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
      expect(matchActionMarkers(s), s).toEqual([])
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
    // No wrapper run here, so the PLAIN branch matches and its label is group 4.
    expect(bodyOf(OPTION_MARKER_RE, pair, 4)).toBe(' A')
    expect(bodyOf(OPTION_ACTION_MARKER_RE, pair, 1)).toBe(' close=B')
    // Nothing anywhere reports the raw action marker as a choice.
    expect(parseOptions(pair).options.some(o => o.includes('OPTION-ACTIONS'))).toBe(false)
  })

  it('parses BOTH when each owns its own line', () => {
    const raw = 'Pick.\n[OPTIONS: Alpha | Beta]\n[OPTION-ACTIONS: close=Nothing else]'
    const parsed = parseOptions(raw)
    expect(parsed.options).toEqual(['Alpha', 'Beta'])
    expect(matchActionMarkers(raw).at(-1)?.[1]).toBe(' close=Nothing else')
    expect(parsed.multi).toBe(true)
    expect(parsed.text).toBe('Pick.')
  })

  it('parses both regardless of which marker comes first', () => {
    const raw = '[OPTION-ACTIONS: close=Nothing else]\n[OPTIONS: Alpha | Beta]'
    expect(parseOptions(raw).options).toEqual(['Alpha', 'Beta'])
    expect(matchActionMarkers(raw).at(-1)?.[1]).toBe(' close=Nothing else')
  })
})

describe('stripping action markers from the displayed text', () => {
  it('removes the marker and the whitespace it sat behind', () => {
    expect(parseOptions('All done.\n\n[OPTION-ACTIONS: close=Nothing else]').text).toBe('All done.')
  })

  it('removes ALL action markers, not just the last', () => {
    // Same reason the content path strips all of them: a stray earlier marker must not leak
    // as raw syntax, even though only the last one supplies the actions.
    const raw = 'a\n[OPTION-ACTIONS: close=One]\nb\n[OPTION-ACTIONS: close=Two]'
    expect(parseOptions(raw).text).toBe('a\n\nb')
    expect(matchActionMarkers(raw).at(-1)?.[1]).toBe(' close=Two')
  })

  it('removes an action marker whatever verb it names', () => {
    // Otherwise the safest input — an action this build does not implement — is the one that
    // leaks its raw syntax to the user.
    expect(parseOptions('Done.\n[OPTION-ACTIONS: reboot=Restart]').text).toBe('Done.')
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

describe('a row the strip would empty keeps its action marker', () => {
  const msg = (content: string): ChatMessage =>
    ({ role: 'assistant', content }) as unknown as ChatMessage
  const ACTION_ONLY = '[OPTION-ACTIONS: close=Done]'

  it('renders the marker rather than an empty row', () => {
    // A CONTENT marker's labels survive the strip as pills, so emptying that row loses
    // nothing. An action marker has no renderer until the chip layer lands, so the same
    // strip left this message with no text at all — erased before anything could show it.
    expect(parseOptions(ACTION_ONLY).text).toBe(ACTION_ONLY)
  })

  it('leaves that row findable, so the screen and the search index agree', () => {
    // The other half of the erasure: the index strips the same span, so the message could
    // not be found either. A marker kept for display must be kept for search.
    expect(searchableText(msg(ACTION_ONLY))).toContain('close=Done')
  })

  it('still strips the marker from a row that carries prose of its own', () => {
    // Negative control: the retention is scoped to rows the strip would empty, so ordinary
    // hand-backs must keep hiding the marker on BOTH surfaces.
    expect(parseOptions(`Done.\n${ACTION_ONLY}`).text).toBe('Done.')
    expect(searchableText(msg(`Done.\n${ACTION_ONLY}`))).not.toContain('close=Done')
  })

  it('still empties a row whose only marker is a CONTENT marker', () => {
    // Negative control: the pills ARE that marker's renderer, so an empty text row is the
    // correct outcome there. Retaining it would render the raw syntax beside its own pills.
    expect(parseOptions('[OPTIONS: Alpha | Beta]').text).toBe('')
    expect(searchableText(msg('[OPTIONS: Alpha | Beta]'))).not.toContain('Alpha')
  })

  it('keeps the row when an EMPTY-labelled content marker renders no pills', () => {
    // A content marker earns the empty row only by RENDERING something. `[OPTIONS:]` is
    // accepted by the grammar — empty labels pass the balance check — while the option
    // split drops them, so keying on the marker's PRESENCE emptied the row with no pills
    // to replace it: the same erasure, reached through the content head instead.
    const raw = '[OPTIONS:] [OPTION-ACTIONS: close=Done]'
    expect(parseOptions(raw).options).toEqual([])
    expect(parseOptions(raw).text).toBe(raw)
    expect(searchableText(msg(raw))).toContain('close=Done')
  })

  it('keeps emptiness keyed on the string BOTH surfaces render, widgets included', () => {
    // The transcript calls `parseOptions` on the whole message, widget and all, so the index
    // must decide emptiness on that same string. Removing the widget FIRST left the marker
    // looking like the row's only content, so the index retained its raw text while the
    // transcript — which still had the widget to render — stripped it: a hit reported
    // against text the highlighter can never mark.
    const raw = '<mcwidget title="x"><div>Green status</div></mcwidget>\n[OPTION-ACTIONS: close=Done]'
    expect(parseOptions(raw).text).not.toContain('close=Done')
    expect(searchableText(msg(raw))).not.toContain('close=Done')
  })

  it('keeps the row when the marker that WOULD render offers nothing', () => {
    // The pills come from the LAST marker, so polling every accepted marker let an earlier
    // one's options vouch for a final one that renders none — a row with neither prose nor
    // pills, which is the erasure this helper exists to prevent.
    const raw = '[OPTIONS: A] [OPTIONS:]'
    expect(parseOptions(raw).options).toEqual([])
    expect(parseOptions(raw).text).toBe(raw)
  })
})

describe('a marker inside verbatim content is documentation, not an offer', () => {
  const msg = (content: string): ChatMessage =>
    ({ role: 'assistant', content }) as unknown as ChatMessage

  it('refuses a marker in a fenced code block, and leaves it on screen', () => {
    // A fence renders as written, so a marker inside one is an agent DOCUMENTING the syntax.
    // Stripping deleted that example from the transcript and, because the index removes the
    // same span, from search as well — the writer's own words gone with no affordance gained.
    const raw = 'Emit it like this:\n```\n[OPTION-ACTIONS: close=Done]\n```\nSparingly.'
    expect(matchActionMarkers(raw)).toEqual([])
    expect(parseOptions(raw).text).toBe(raw)
    expect(searchableText(msg(raw))).toContain('close=Done')
  })

  it('refuses a marker inside a widget body', () => {
    const raw = '<mcwidget title="x">\n[OPTION-ACTIONS: close=Done]\n</mcwidget>'
    expect(matchActionMarkers(raw)).toEqual([])
    expect(parseOptions(raw).text).toBe(raw)
  })

  it('refuses one in an UNCLOSED fence, which a streaming turn is', () => {
    const raw = 'Mid-stream:\n```\n[OPTION-ACTIONS: close=Done]'
    expect(matchActionMarkers(raw)).toEqual([])
    expect(parseOptions(raw).text).toBe(raw)
  })

  it('still accepts one OUTSIDE the fence in the same message', () => {
    // Negative control: the refusal is scoped to the verbatim span, not to any message that
    // happens to contain one, so a real hand-back after a fenced example still works.
    const raw = '```\n[OPTION-ACTIONS: close=Shown]\n```\n[OPTION-ACTIONS: close=Real]'
    expect(matchActionMarkers(raw).map(m => m[1])).toEqual([' close=Real'])
    expect(parseOptions(raw).text).toBe('```\n[OPTION-ACTIONS: close=Shown]\n```')
  })
})

describe('prose that merely DISCUSSES the syntax is unchanged', () => {
  // The acceptance criterion behind refusing a marker that trailing prose follows: an
  // agent writing docs about the feature must not emit a live control.
  it.each([
    ['head named mid-sentence', 'The marker is [OPTION-ACTIONS: close=label] and it closes the tab.'],
    ['head with no body at all', 'Emit [OPTION-ACTIONS: followed by entries.'],
    ['bare head in a sentence', 'The [OPTION-ACTIONS head is distinct from [OPTIONS.'],
    ['inline code fragment', 'Write `[OPTION-ACTIONS: close=X]` on its own line.'],
    ['marker then trailing prose', '[OPTION-ACTIONS: close=X] — but only when the note landed.'],
  ])('%s', (_name, prose) => {
    const parsed = parseOptions(prose)
    expect(matchActionMarkers(prose), prose).toEqual([])
    expect(parsed.options).toEqual([])
    expect(parsed.text).toBe(prose)
  })

  it('offers nothing for a label-shaped word that only LOOKS like the head', () => {
    for (const s of ['[OPTIONAL: a | b]', '[OPTION-ACTION: close=X]', '[OPTIONACTIONS: close=X]']) {
      expect(matchActionMarkers(s), s).toEqual([])
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

  it('recognises the marker and drops its raw text once it completes', () => {
    const raw = 'Done.\n[OPTION-ACTIONS: close=Nothing else]'
    expect(parseOptions(raw).text).toBe('Done.')
    expect(matchActionMarkers(raw).at(-1)?.[1]).toBe(' close=Nothing else')
    // And the streaming probe agrees, so there is no frame where both are visible.
    expect(streaming(raw)).toBe('Done.')
  })

  it('does not hold ordinary prose that happens to start with a bracket', () => {
    // The mid-head branch requires consistent casing and a whitespace boundary, so these
    // are released rather than held for the width of the longer head.
    expect(streaming('See [Optional')).toBe('See [Optional')
    expect(streaming('index arr[0')).toBe('index arr[0')
    expect(streaming('See [OPTION-X')).toBe('See [OPTION-X')
  })
})
