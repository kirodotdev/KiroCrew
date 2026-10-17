import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, it, expect } from 'vitest'
import { ASK_ANSWERED_HEADER, ASK_MAX_ANSWER_LEN, ASK_PAIR_SEPARATOR, MAX_TOOL_RESULT_CHARS, NATIVE_MAX_ANSWER_LEN } from '../utils/askQuestionTool'

// Blocking-card cap stays synchronized with validation.py's derived server bound.

const SRC = path.resolve(__dirname, '../../../src/kiro_crew')

function pyInt(source: string, name: string): number {
  const m = source.match(new RegExp(`^${name} = (\\d+)$`, 'm'))
  if (!m) throw new Error(`${name} not found`)
  return Number(m[1])
}

describe('ASK_MAX_ANSWER_LEN', () => {
  it('mirrors the bound validation.py derives from the tool-result cap', () => {
    const validation = readFileSync(path.join(SRC, 'validation.py'), 'utf-8')
    const directive = readFileSync(path.join(SRC, 'session_directive.py'), 'utf-8')
    const cap = pyInt(directive, 'MAX_TOOL_RESULT_CHARS')
    const questions = pyInt(validation, '_ASK_MAX_QUESTIONS')
    const questionLen = pyInt(validation, '_ASK_MAX_QUESTION_LEN')
    expect(validation).toContain(`_ASK_ANSWERS_HEADER = "${ASK_ANSWERED_HEADER}"`)
    // Newline, the quotes around key and answer, and the separator between them.
    expect(validation).toContain(`_ASK_PAIR_SEPARATOR = "${ASK_PAIR_SEPARATOR}"`)
    const lineOverhead = `\n""${ASK_PAIR_SEPARATOR}""`.length + questionLen
    const expected = Math.floor((cap - ASK_ANSWERED_HEADER.length - questions * lineOverhead) / questions)
    expect(ASK_MAX_ANSWER_LEN).toBe(expected)
  })

  it('selects the blocking or native bound for the custom-answer input', () => {
    const card = readFileSync(path.resolve(__dirname, '../components/QuestionCard.tsx'), 'utf-8')
    expect(NATIVE_MAX_ANSWER_LEN).toBe(2000)
    expect(card).toContain('const answerLimit = askId ? ASK_MAX_ANSWER_LEN : NATIVE_MAX_ANSWER_LEN')
    expect(card).toContain('maxLength={answerLimit}')
    expect(card).not.toMatch(/maxLength=\{\d+\}/)
  })
})

describe('MAX_TOOL_RESULT_CHARS', () => {
  it('mirrors the Python tool-result cap', () => {
    const directive = readFileSync(path.join(SRC, 'session_directive.py'), 'utf-8')
    expect(MAX_TOOL_RESULT_CHARS).toBe(pyInt(directive, 'MAX_TOOL_RESULT_CHARS'))
  })
})
