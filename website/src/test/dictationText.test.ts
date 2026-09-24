import { describe, expect, it } from 'vitest'
import {
  dictationSeparator, dictationSpliceSpan, joinTranscript, locateDictationSpan, spliceDictationText, transcriptTail,
  userEditTouchesRun,
} from '../lib/dictationText'

describe('dictation text boundaries', () => {
  it('inserts Chinese at the cursor without artificial spaces on either side', () => {
    expect(spliceDictationText('请处理', '继续', { start: 1, end: 1 })).toEqual({ value: '请继续处理', caret: 3 })
  })

  it('replaces the selected region and restores the caret after Chinese speech', () => {
    expect(spliceDictationText('请删除处理', '继续', { start: 1, end: 3 })).toEqual({ value: '请继续处理', caret: 3 })
  })

  it('keeps English words separated without moving the caret past the existing suffix', () => {
    expect(spliceDictationText('Pleaseprocess', 'continue', { start: 6, end: 6 })).toEqual({ value: 'Please continue process', caret: 15 })
  })

  it('dictates immediately before an existing comma without moving or replacing the suffix', () => {
    expect(spliceDictationText('hello, world', 'there', { start: 5, end: 5 })).toEqual({
      value: 'hello there, world', caret: 11,
    })
  })

  it.each(['.', '!', '?', '; next', ': next', ')', ']', '}'])(
    'keeps existing closing punctuation %s attached to inserted speech', (suffix) => {
      expect(spliceDictationText(`hello${suffix}`, 'there', { start: 5, end: 5 })).toEqual({
        value: `hello there${suffix}`, caret: 11,
      })
    },
  )

  it('joins recognized punctuation to the preceding utterance without swallowing following words', () => {
    expect(joinTranscript(['hello', ', world', '!'])).toBe('hello, world!')
    expect(joinTranscript(['hello', 'there'])).toBe('hello there')
  })

  it('preserves existing spaces, newlines and tabs around the insertion', () => {
    expect(spliceDictationText('请\n\t处理', '继续', { start: 2, end: 2 })).toEqual({ value: '请\n继续\t处理', caret: 4 })
    expect(spliceDictationText('Please  process', 'continue', { start: 7, end: 7 }).value).toBe('Please continue process')
  })

  it('uses the same joining rule when the composer has never had a caret', () => {
    expect(spliceDictationText('请', '继续', null)).toEqual({ value: '请继续', caret: 3 })
    expect(spliceDictationText('Please', 'continue', null).value).toBe('Please continue')
  })

  it('does not delete a selected draft when a hypothesis is withdrawn', () => {
    expect(spliceDictationText('请处理', '', { start: 0, end: 3 })).toEqual({ value: '请处理', caret: 0 })
  })

  it('joins committed utterances and partials without deduplicating intentional repetition', () => {
    expect(joinTranscript(['继续', '继续', '，请处理。'])).toBe('继续继续，请处理。')
    expect(joinTranscript(['Please', 'continue', 'processing.'])).toBe('Please continue processing.')
    expect(dictationSeparator('𠀀', '𠀁')).toBe('')
  })

  it('keeps a mixed-script caption tail instead of discarding it before an English space', () => {
    expect(transcriptTail('旧内容新的字幕 hello world', 16)).toBe('新的字幕 hello world')
    expect(transcriptTail('an unfinishedword hello', 12)).toBe('hello')
    expect(transcriptTail('an unfinishedword你好', 10)).toBe('你好')
    expect(transcriptTail('unfinished, hello', 9)).toBe('hello')
    expect(transcriptTail('unfinished, hello', 7)).toBe('hello')
  })

  it('respects the UTF-16 caption budget without splitting supplementary Han characters', () => {
    expect(transcriptTail('𠀀𠀁𠀂', 5)).toBe('𠀁𠀂')
    expect(transcriptTail('a singleword', 4)).toBe('word')
    expect(transcriptTail('你好', 2)).toBe('你好')
    expect(transcriptTail('你好', 0)).toBe('')
  })
})

describe('a dictation splice described as a span', () => {
  it('spans exactly the run the write added, with its far-side separator apart', () => {
    const span = dictationSpliceSpan('Pleaseprocess', 'continue', { start: 6, end: 6 })
    const { value, caret } = spliceDictationText('Pleaseprocess', 'continue', { start: 6, end: 6 })
    expect(value).toBe('Please continue process')
    expect(span).toEqual({ start: 6, end: 15, text: ' continue', trail: ' ', replaced: '' })
    expect(value.slice(span!.start, span!.end)).toBe(span!.text)
    // `end` is the caret the splice reports, so a caller holding the value it
    // wrote can record the span without measuring it again.
    expect(span!.end).toBe(caret)
  })

  it('reports the selected words the write consumed, which removing the span would not give back', () => {
    const span = dictationSpliceSpan('Please review the plan', 'release', { start: 7, end: 13 })
    expect(spliceDictationText('Please review the plan', 'release', { start: 7, end: 13 }).value)
      .toBe('Please release the plan')
    expect(span!.replaced).toBe('review')
  })

  it('spans the appended run when the composer never had a caret', () => {
    expect(dictationSpliceSpan('请', '继续', null))
      .toEqual({ start: 1, end: 3, text: '继续', trail: '', replaced: '' })
  })

  it('describes no span for a withdrawn hypothesis, since nothing was written', () => {
    expect(dictationSpliceSpan('请处理', '', { start: 0, end: 3 })).toBeNull()
  })
})

describe('finding a written span in a value the user has edited', () => {
  const written = 'ask about the rollout and the dates'
  const span = { start: 21, end: 35, text: ' and the dates' }

  it('shifts the span by the edit when the typing happened before it', () => {
    expect(locateDictationSpan(written, `PLEASE ${written}`, span)).toBe(28)
  })

  it('leaves the span where it was when the typing happened after it', () => {
    // Separated from the span by text neither side touched, so only one alignment
    // explains the pair and the span is still exactly where it was written. This is
    // the ordinary far-side rollback.
    expect(locateDictationSpan(`${written} next week`, `${written} next week today`, span)).toBe(21)
    // A deletion at the boundary resolves too: it put no characters in, so it cannot
    // have written the run.
    expect(locateDictationSpan(`${written} today`, written, span)).toBe(21)
    // But an INSERTION that begins exactly where the span ends is the far-side twin of
    // the near-side ambiguity -- the typing could have started one character earlier
    // and rewritten the run's own tail -- so it refuses.
    expect(locateDictationSpan(written, `${written} today`, span)).toBe(-1)
  })

  it('finds the span untouched in a value nobody edited', () => {
    expect(locateDictationSpan(written, written, span)).toBe(21)
  })

  it('refuses when the edit reached into the span, so those characters are not only the machine\'s', () => {
    expect(locateDictationSpan(written, 'ask about the rollout and the DAYS', span)).toBe(-1)
    expect(locateDictationSpan(written, 'ask about the rollout instead', span)).toBe(-1)
  })

  it('refuses rather than take an identical phrase the user typed themselves', () => {
    // The run is gone from where it was written and the same words sit elsewhere.
    expect(locateDictationSpan(written, 'ask and the dates about the rollout urgently', span)).toBe(-1)
  })

  it('refuses when the typing repeats the run, so both readings hold the same text', () => {
    // Typing `ship ` in front of a dictated `ship` and typing ` ship` after it
    // produce the same value, and the run's text then sits at BOTH offsets. One
    // of the two copies is the user's and nothing here says which.
    const ship = { start: 7, end: 11, text: 'ship' }
    expect(locateDictationSpan('please ship', 'please ship ship', ship)).toBe(-1)
    // One copy only, so the same shape still resolves.
    expect(locateDictationSpan('please ship', 'kindly please ship', ship)).toBe(14)
  })

  it('refuses a run written up against a copy of itself, whichever copy went', () => {
    // Dictating a word the draft already has beside it. Deleting the machine's
    // copy and deleting the user's own leave the same value, so their word slides
    // into the run's offsets and reads as the run. Decided on the written value
    // alone, because no reading of the current one can tell the two apart.
    const dup = { start: 6, end: 13, text: ' review' }
    const written = 'please review review the plan'
    expect(locateDictationSpan(written, 'please review the plan', dup)).toBe(-1)
    // Also when they typed something else in the same breath, which an exact
    // "the value is the base again" test would let through.
    expect(locateDictationSpan(written, 'please review the plan!', dup)).toBe(-1)
    // Untouched, and now RESOLVED: with nothing edited the span is still where it
    // was written, so there is nothing to infer -- and the two deletions produce
    // the same draft here anyway, so refusing would only leave the speech behind.
    expect(locateDictationSpan(written, written, dup)).toBe(6)
  })

  it('resolves when the next word merely starts with the run', () => {
    // Dictating `send` in front of the draft's `sending`. The window used to match
    // the first four letters of the neighbour and refuse, but deleting the
    // neighbour gives `send ing the report` and deleting the run gives
    // `sending the report`: the two are distinguishable, so nothing was ambiguous.
    const send = { start: 0, end: 4, text: 'send', trail: ' ' }
    const written = 'send sending the report'
    expect(locateDictationSpan(written, written, send)).toBe(0)
    expect(locateDictationSpan(written, `${written} today`, send)).toBe(0)
    // A neighbour that IS the word, on the other hand, stays ambiguous.
    const dup = { start: 0, end: 4, text: 'send', trail: ' ' }
    expect(locateDictationSpan('send send the report', 'send the report', dup)).toBe(-1)
  })

  it('finds the abutting copy past the separators the write itself added', () => {
    // The run's own separator sits between the two copies, so a window measured
    // from the span's end lands one character off and reads ` the pla` for
    // `the plan`. Dictating at the very start of the draft: the write adds its
    // separator BEHIND the run, and the user then removes the duplicate.
    const lead = { start: 0, end: 8, text: 'the plan', trail: ' ' }
    expect(locateDictationSpan('the plan the plan is ready', 'the plan is ready', lead)).toBe(-1)
    // Mid-draft, same shape.
    const mid = { start: 10, end: 14, text: 'ship', trail: ' ' }
    expect(locateDictationSpan('we should ship ship it', 'we should ship it', mid)).toBe(-1)
    // More than one space between them is still the same ambiguity.
    expect(locateDictationSpan('the plan  the plan is ready', 'the plan is ready', lead)).toBe(-1)
    // A copy the draft held BEFORE the run needs no rule of its own: it is
    // identical to the run, so removing it leaves a prefix that runs past the
    // deletion and the offsets place the edit on neither side.
    const behind = { start: 14, end: 20, text: 'review' }
    expect(locateDictationSpan('please review review the plan', 'please review the plan', behind)).toBe(-1)
    expect(locateDictationSpan('please review review', 'please review', behind)).toBe(-1)
  })

  it('refuses the abutting copy on an untouched value too, once something was displaced', () => {
    // The spared case above rests on the two deletions agreeing -- true of the
    // REMOVAL, but the discard reinserts what the dictation displaced at the offset
    // it removed from, and there the two copies are not interchangeable. An edit
    // that lands back on the same bytes leaves no trace, so a value that still
    // reads as written is no proof the run is where it was put: dictate over a
    // word the draft repeats, then type a copy in front and delete the trailing
    // one, and the restore puts the displaced word one copy off.
    const dup = { start: 6, end: 13, text: ' review', replaced: ' skim' }
    const written = 'please review review the plan'
    expect(locateDictationSpan(written, written, dup)).toBe(-1)
    // The reported sequence, in the script that makes it a single character:
    // `错` selected in `错啊`, `啊` dictated, `啊` typed in front, the trailing one
    // deleted. Located at 0 the restore reads `错啊`; the run is the SECOND copy.
    const cjk = { start: 0, end: 1, text: '啊', replaced: '错' }
    expect(locateDictationSpan('啊啊', '啊啊', cjk)).toBe(-1)
    // Nothing displaced keeps the old answer: the two deletions really do agree,
    // and refusing would leave the speech in a composer asked to be rid of it.
    expect(locateDictationSpan('啊啊', '啊啊', { ...cjk, replaced: '' })).toBe(0)
  })

  it('refuses when the deletion could equally have come from in front of the span', () => {
    // The prefix walk finds ONE alignment. A dictated phrase that repeats a word the
    // draft continues with makes it run THROUGH the span: `plan plan` dictated in
    // front of a draft `plan`, then the first dictated `plan ` deleted, leaves a
    // value whose prefix still agrees past the span's end. The edit then reads as
    // one made beyond the span while it was made in front of it, the span reads as
    // whole at offset 0, and taking it deletes the word the user authored -- the
    // composer is left EMPTY, with nothing to undo it.
    const periodic = { start: 0, end: 9, text: 'plan plan', trail: ' ', replaced: '' }
    expect(locateDictationSpan('plan plan plan', 'plan plan', periodic)).toBe(-1)
    // The same shape one word longer, so the refusal is not an artefact of the
    // span reaching the value's start.
    const periodic2 = { start: 7, end: 16, text: 'plan plan', trail: ' ', replaced: '' }
    expect(locateDictationSpan('please plan plan plan', 'please plan plan', periodic2)).toBe(-1)
    // A deletion after the span whose characters do NOT repeat past the boundary is
    // still placed: one alignment explains the pair, so the span is intact.
    const ship = { start: 0, end: 4, text: 'ship', trail: ' ', replaced: '' }
    expect(locateDictationSpan('ship the box today', 'ship the box', ship)).toBe(0)
  })

  it('refuses when the shifted offset and the written one both hold the run', () => {
    // An edit wholly BEFORE the span shifts it by the length delta -- but when the
    // value repeats the run, the offset it shifts to and the offset it came from
    // both read as the run, and nothing in the value says which copy the machine
    // wrote. This is the half the alignment slide does not cover: there the edit
    // really is before the span, so no reading of the boundary is in doubt.
    // The repeat sits far enough from the run that the abutting-copy rule does not
    // see it, so this offset pair is the only thing that answers.
    const far = { start: 8, end: 10, text: 'ab', trail: '', replaced: '' }
    expect(locateDictationSpan('wxyzDDDDabQQab', 'wxyzabQQab', far)).toBe(-1)
    // Without the second copy only the shifted offset holds, so the span is placed.
    expect(locateDictationSpan('wxyzDDDDabQQxy', 'wxyzabQQxy', far)).toBe(4)
  })

  it('refuses an abutting copy even when the edit itself was somewhere harmless', () => {
    // The copy makes the run's position unknowable from the value, whatever the
    // edit was: here the user only typed a character at the very end, so the
    // alignment is unique and every offset check passes -- the copy is the only
    // thing standing between the discard and a deletion it cannot justify. A window
    // measured at the span's own end would read the separator the write added and
    // miss the copy entirely.
    const lead = { start: 0, end: 8, text: 'the plan', trail: ' ', replaced: '' }
    expect(locateDictationSpan('the plan the plan is ready', 'the plan the plan is ready!', lead)).toBe(-1)
  })

  it('still removes the run when the draft repeats the word somewhere else', () => {
    // A copy that is not beside the span leaves the two deletions distinguishable,
    // so refusing here would strand speech the discard can safely take.
    const far = { start: 12, end: 17, text: ' ship' }
    expect(locateDictationSpan('ship the box ship', 'ship the box ship', far)).toBe(12)
    // Its edited form is now refused for a different reason -- the inserted `es` ends
    // where the span lands, which the case below is about -- so the shape that keeps
    // this one honest is the untouched value, plus the insertion far from the span in
    // the first case of this suite.
  })

  it('refuses when the typing begins where the span ends, so authored letters survive', () => {
    // Select the dictated `plan`, type `planet`. The run's characters are still there
    // by value, and the arithmetic used to read that as "the edit is past the span",
    // so the discard removed `plan` and left the user holding `et`.
    const plan = { start: 0, end: 4, text: 'plan', trail: '', replaced: '' }
    expect(locateDictationSpan('plan', 'planet', plan)).toBe(-1)
    // Mid-draft, same shape.
    const mid = { start: 7, end: 11, text: 'plan', trail: '', replaced: '' }
    expect(locateDictationSpan('please plan', 'please planet', mid)).toBe(-1)
  })

  it('refuses when the user\'s own typing ends where the span lands', () => {
    // An insertion that runs right up against the landing offset is equally well
    // explained by the typing having continued THROUGH the run: replacing a dictated
    // `plan` with `new plan` leaves exactly what typing `new ` in front of it leaves,
    // and under the first reading the `plan` in the composer is the user's own. The
    // rollback would delete a word they authored and leave `new `, with nothing to
    // put it back.
    const plan = { start: 0, end: 4, text: 'plan', trail: '', replaced: '' }
    expect(locateDictationSpan('plan', 'new plan', plan)).toBe(-1)
    // Not an artefact of the span covering the whole value.
    const mid = { start: 7, end: 11, text: 'plan', trail: '', replaced: '' }
    expect(locateDictationSpan('please plan', 'please new plan', mid)).toBe(-1)
    // An insertion abutting the span is the ONLY shape this costs. A deletion puts no
    // characters in, so it cannot have written the run.
    const dele = { start: 8, end: 10, text: 'ab', trail: '', replaced: '' }
    expect(locateDictationSpan('wxyzDDDDabQQxy', 'wxyzabQQxy', dele)).toBe(4)
    // The far side answers the same way, and for the same reason: typing that begins
    // where the run ends could have begun a character earlier and rewritten its tail.
    const ship = { start: 7, end: 11, text: 'ship', trail: '', replaced: '' }
    expect(locateDictationSpan('please ship', 'please ship today', ship)).toBe(-1)
    // With one untouched character between the run and the typing, neither side is in
    // doubt any more and the run is placed.
    expect(locateDictationSpan('please ship now', 'please ship now today', ship)).toBe(7)
  })
})

describe('did one reported edit reach the dictated run', () => {
  // The run is `ship` at 7..11 of `please ship` throughout.
  const RUN: [number, number] = [7, 11]
  const touches = (before: string, after: string) => userEditTouchesRun(before, after, ...RUN)

  it('answers no when nothing changed', () => {
    expect(touches('please ship', 'please ship')).toBe(false)
  })

  it('answers yes when the run itself was deleted', () => {
    // The half of a retyping that the values can no longer show: after the second half
    // the pair is identical to an untouched run, so this is the observation that lasts.
    expect(touches('please ship', 'please ')).toBe(true)
  })

  it('answers yes when the run was replaced in place', () => {
    expect(touches('please ship', 'please sail')).toBe(true)
  })

  it('answers yes for typing strictly inside the run', () => {
    expect(touches('please ship', 'please shXip')).toBe(true)
  })

  it('answers no for typing that stops at either edge of the run', () => {
    // Typing in front of the run or after it leaves the run's own characters alone.
    // Those two shapes are the locator's to judge, and it refuses whenever the pair
    // is ambiguous -- a touch would be the wrong answer here, not a safer one.
    expect(touches('please ship', 'please new ship')).toBe(false)
    expect(touches('please ship', 'please ship today')).toBe(false)
  })

  it('answers no for an edit that never comes near the run', () => {
    expect(touches('please ship', 'PLEASE please ship')).toBe(false)
    expect(touches('please ship it', 'please ship')).toBe(false)
  })

  it('answers yes when a deletion takes the run\'s last character with it', () => {
    expect(touches('please ship', 'please shi')).toBe(true)
  })

  it('answers no when the typing repeats the run just past its end', () => {
    // The alignment the locator finds here is ambiguous, and it refuses on that
    // ground. The region between the common prefix and suffix is empty at the run's
    // far edge, so this is not reported as a touch: one judgement, in one place.
    expect(touches('please ship', 'please ship ship')).toBe(false)
  })
})
