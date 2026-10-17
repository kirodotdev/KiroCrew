import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { blockedLinkQueryTail } from '../components/RedactionCards'

// A link removed in parts (a presigned link split at its query) leaves a query
// tail glued to the placeholder. The chip takes that tail in; any other text
// after a placeholder stays as written.

const HOST = 'my-bucket.s3.us-east-1.amazonaws.com'
const PH = `[REDACTED: suspicious URL to ${HOST}]`
const record = { domain: HOST, rule: 'exfil_hard_credential', path: '/a', query_chars: 9, url: null, url_withheld: 'credential' as const }
const TAIL = '&X-Amz-Date=20261008T000000Z&X-Amz-Signature=' + 'b'.repeat(64)

describe('blockedLinkQueryTail', () => {
  it('takes a glued query parameter up to whitespace', () => {
    expect(blockedLinkQueryTail(`${TAIL} and more`)).toBe(TAIL)
    expect(blockedLinkQueryTail('?a=1&b=2')).toBe('?a=1&b=2')
  })

  it('leaves the punctuation that closes a sentence', () => {
    expect(blockedLinkQueryTail(`${TAIL}.`)).toBe(TAIL)
    expect(blockedLinkQueryTail('&a=1),')).toBe('&a=1')
  })

  it('stops at a square bracket, so a following credential tag stays its own', () => {
    expect(blockedLinkQueryTail('&k=[REDACTED: credential]%2Fx')).toBe('&k=')
  })

  it('takes nothing that is not a glued query parameter', () => {
    for (const after of ['', ' &a=1', '?', '? yes', '&', '& more', '&amp;', '!', ', then', '?=1', '/path?a=1']) {
      expect(blockedLinkQueryTail(after)).toBe('')
    }
  })
})

describe('a placeholder followed by a query tail', () => {
  it('renders the chip with no tail text beside it and keeps the prose after', () => {
    const { container, getByTestId } = render(
      <MarkdownRenderer content={`See ${PH}${TAIL} for the report.`} blockedLinks={[record]} slotKey="s1" />)
    expect(getByTestId('blocked-link-chip')).toBeTruthy()
    const text = container.textContent ?? ''
    expect(text).not.toContain('X-Amz')
    expect(text).toContain(' for the report.')
    expect(container.querySelector('a')).toBeNull()
  })

  it('keeps the tail as written when the host has no record, so no chip renders', () => {
    const { container, queryByTestId } = render(
      <MarkdownRenderer content={`See ${PH}${TAIL} now`} blockedLinks={[]} slotKey="s1" />)
    expect(queryByTestId('blocked-link-chip')).toBeNull()
    expect(container.textContent).toContain(`${PH}${TAIL} now`)
    expect(container.querySelector('a')).toBeNull()
  })

  it('leaves text after the placeholder alone when it is not a glued query', () => {
    const { container } = render(
      <MarkdownRenderer content={`Open ${PH}? Then ${PH} &a=1 stays.`} blockedLinks={[record]} slotKey="s1" />)
    expect(container.textContent).toMatch(/\? Then /)
    expect(container.textContent).toContain(' &a=1 stays.')
  })

  it('still renders a credential tag that follows the tail', () => {
    const { getAllByTestId, getByTestId } = render(
      <MarkdownRenderer
        content={`See ${PH}&k=[REDACTED: credential] end`}
        blockedLinks={[record]}
        redactions={[{ ordinal: 0, rule: 'aws_secret_access_key', label: 'aws_secret_access_key = ', source: null, view_command: null, profile_command: null }]}
        slotKey="s1"
      />)
    expect(getByTestId('blocked-link-chip')).toBeTruthy()
    expect(getAllByTestId('credential-tag')).toHaveLength(1)
  })
})
