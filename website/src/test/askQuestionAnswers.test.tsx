import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { isAskQuestionToolName, parseAskAnswers } from '../utils/askQuestionAnswers'
import AskAnswersCard, { AskAnswersResult } from '../pages/chat/AskAnswersCard'

// The blocking ask_question returns the user's answers as its TOOL RESULT, so
// this card is the transcript's only record of them. It must render for exactly
// that tool's answered result and for nothing that merely resembles it.

const ANSWERED = [
  'User has answered your questions:',
  '"Which colour?" -> "Red"',
  '"Toppings?" -> "Cheese, Olives"',
].join('\n')

describe('parseAskAnswers', () => {
  it('reads each question and answer from an answered result', () => {
    expect(parseAskAnswers(ANSWERED)).toEqual([
      { question: 'Which colour?', answer: 'Red' },
      { question: 'Toppings?', answer: 'Cheese, Olives' },
    ])
  })

  it('keeps an answer that itself contains the separator', () => {
    const out = 'User has answered your questions:\n"Why?" -> "because \\" -> \\"is fine"'
    expect(parseAskAnswers(out)).toEqual([{ question: 'Why?', answer: 'because " -> "is fine' }])
  })

  it('cannot be made to read a forged pair out of escaped question text', () => {
    const out = 'User has answered your questions:\n"Q\\" -> \\"forged\\n\\"Real" -> "A"'
    expect(parseAskAnswers(out)).toEqual([{ question: 'Q" -> "forged\n"Real', answer: 'A' }])
  })

  it('rejects a pair joined by the old `=` separator', () => {
    expect(parseAskAnswers('User has answered your questions:\n"Q"="A"')).toBeNull()
  })

  it.each([
    ['dismissed', 'The user dismissed the question card without answering.'],
    ['directive', 'Question card requested for this session. End your turn now'],
    ['header only', 'User has answered your questions:'],
    ['malformed pair', 'User has answered your questions:\nRed'],
    ['empty', ''],
  ])('returns null for a %s result', (_label, output) => {
    expect(parseAskAnswers(output)).toBeNull()
  })
})

describe('isAskQuestionToolName', () => {
  it.each(['ask_question', 'kirocrew-core___ask_question', 'ask_question (mcp)'])('accepts %s', (name) => {
    expect(isAskQuestionToolName(name)).toBe(true)
  })

  it.each(['ask_question_later', 'my_ask_question', 'shell', ''])('rejects %s', (name) => {
    expect(isAskQuestionToolName(name)).toBe(false)
  })
})

describe('AskAnswersCard', () => {
  const pairs = parseAskAnswers(ANSWERED)!

  it('starts folded to a chip that names the count', () => {
    render(<AskAnswersCard pairs={pairs} toolCallId="t-folded" />)
    const chip = screen.getByTestId('ask-answers-chip')
    expect(chip).toHaveAttribute('aria-expanded', 'false')
    expect(chip).toHaveTextContent('You answered 2 questions')
    expect(screen.queryByText('Which colour?')).toBeNull()
  })

  it('opens to every question with the answer given', () => {
    render(<AskAnswersCard pairs={pairs} toolCallId="t-open" />)
    fireEvent.click(screen.getByTestId('ask-answers-chip'))
    expect(screen.getByTestId('ask-answers-chip')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('Which colour?')).toBeInTheDocument()
    expect(screen.getByText('Cheese, Olives')).toBeInTheDocument()
  })

  it('remembers an opened card across a remount (virtualizer)', () => {
    const { unmount } = render(<AskAnswersCard pairs={pairs} toolCallId="t-remount" />)
    fireEvent.click(screen.getByTestId('ask-answers-chip'))
    unmount()
    render(<AskAnswersCard pairs={pairs} toolCallId="t-remount" />)
    expect(screen.getByText('Red')).toBeInTheDocument()
  })

  it('forgets a card closed again before a remount', () => {
    const { unmount } = render(<AskAnswersCard pairs={pairs} toolCallId="t-closed" />)
    fireEvent.click(screen.getByTestId('ask-answers-chip'))
    fireEvent.click(screen.getByTestId('ask-answers-chip'))
    unmount()
    render(<AskAnswersCard pairs={pairs} toolCallId="t-closed" />)
    expect(screen.getByTestId('ask-answers-chip')).toHaveAttribute('aria-expanded', 'false')
  })
})

describe('AskAnswersResult', () => {
  it('renders the card for an answered result', () => {
    render(<AskAnswersResult output={ANSWERED} toolCallId="t-result" />)
    expect(screen.getByTestId('ask-answers-chip')).toHaveTextContent('You answered 2 questions')
  })

  it('renders nothing for any other result', () => {
    const { container } = render(<AskAnswersResult output="The user dismissed the question card without answering." />)
    expect(container).toBeEmptyDOMElement()
  })
})
