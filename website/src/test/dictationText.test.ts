import { describe, expect, it } from 'vitest'
import { dictationSeparator, joinTranscript, spliceDictationText, transcriptTail } from '../lib/dictationText'

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
