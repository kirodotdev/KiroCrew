import { describe, expect, it } from 'vitest'
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import { render } from '@testing-library/react'
import { approvalNotificationBody, APPROVAL_COMMAND_TAG } from '../lib/approvalNotificationBody'
import { stripMd } from '../components/notifications/notifMeta'
import MarkdownRenderer from '../components/MarkdownRenderer'

const commands = [
  'rm -rf *cache*',
  '_tmp_',
  '~~name~~',
  '~/.ssh/id_rsa',
  'echo hi > out.log',
  'echo ```; rm -rf *cache*',
  'echo ````; rm -rf *cache*',
]

describe('approvalNotificationBody', () => {
  it.each([undefined, '**Reason:** review both commands'])('preserves multiline approval commands through composition: %s', purpose => {
    const command = 'echo safe\nrm -rf target'
    const body = approvalNotificationBody('agent', command, purpose)
    const code = unified().use(remarkParse).parse(body).children.find(node => node.type === 'code')
    expect(code?.value).toBe(command)
    expect(stripMd(body)).toBe('Source: agent · ' + command + (purpose ? ' · Reason: review both commands' : ''))
    expect(stripMd(body)).toContain('echo safe\nrm -rf target')
  })

  it('preserves blank lines and indentation through approval composition', () => {
    const command = '\n  echo safe  \n\n\trm -rf target\n'
    const body = approvalNotificationBody('cron', command, 'Review this')
    const code = unified().use(remarkParse).parse(body).children.find(node => node.type === 'code')
    expect(code?.value).toBe(command)
    expect(stripMd(body)).toBe('Source: cron · ' + command + ' · Review this')
  })

  it.each(commands)('preserves the authorized command in plain-text previews: %s', command => {
    expect(stripMd(approvalNotificationBody('agent', command)))
      .toBe(`Source: agent · ${command}`)
    expect(stripMd(approvalNotificationBody('cron', command, '**Reason:** cleanup')))
      .toBe(`Source: cron · ${command} · Reason: cleanup`)
  })

  it.each([undefined, ''])('omits the fence for absent or empty input: %s', command => {
    expect(approvalNotificationBody(undefined, command)).toBe('**Source:** agent')
    expect(approvalNotificationBody('cron', command, 'Review this'))
      .toBe('**Source:** cron\n\n\n\nReview this')
    expect(stripMd(approvalNotificationBody('cron', command, 'Review this'))).toBe('Source: cron · Review this')
  })

  it.each([
    ['plain', '```'],
    ['echo ```', '````'],
    ['echo ````', '`````'],
    ['echo ``; echo `````; echo `', '``````'],
  ])('chooses a fence longer than every command run: %s', (command, fence) => {
    expect(approvalNotificationBody('agent', command))
      .toBe(`**Source:** agent\n\n${fence}${APPROVAL_COMMAND_TAG}\n${command}\n${fence}`)
  })

  it('tags the fence with the dashboard-own wrap tag, which the code block shows as its label', () => {
    // jsdom has no highlighter worker, so the soft-wrap itself is a browser
    // fact the capture harness asserts; here the renderer must at least see the
    // tag as the fence language and keep the command verbatim under it.
    const wide = 'echo ' + 'x'.repeat(400) + '; rm -rf target'
    const { container } = render(<MarkdownRenderer content={approvalNotificationBody('agent', wide)} />)
    expect(container.querySelector('.code-block')?.textContent).toContain(APPROVAL_COMMAND_TAG)
    expect(container.querySelector('.code-block [role="region"]')?.textContent).toBe(wide)
  })

  it.each(commands)('renders the literal command as one detail-panel code block: %s', command => {
    const { container } = render(<MarkdownRenderer content={approvalNotificationBody('agent', command, 'Review this')} />)
    expect(container.querySelectorAll('.code-block')).toHaveLength(1)
    expect(container.querySelector('.code-block [role="region"]')?.textContent).toBe(command)
    expect(container.querySelectorAll('pre')).toHaveLength(1)
    expect(container.querySelectorAll('p')).toHaveLength(2)
    expect(container.querySelectorAll('p')[0].textContent).toBe('Source: agent')
    expect(container.querySelectorAll('p')[1].textContent).toBe('Review this')
  })
})
