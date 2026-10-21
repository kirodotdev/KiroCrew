/**
 * A `<mcwidget>` whose body is markdown, not HTML, renders as markdown under its
 * title instead of raw `**` and `|` text in a frame. An HTML body still goes to
 * the widget frame.
 */
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'

vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: true }),
}))
vi.mock('../components/WidgetFrame', () => ({
  default: ({ title }: { title?: string }) => <div data-testid="widget-frame">{title}</div>,
}))

import MarkdownRenderer from '../components/MarkdownRenderer'

const MD_WIDGET = [
  'Here is where things stand.',
  '',
  '<mcwidget title="Task board">',
  '',
  '**Needs you:** Nothing right now.',
  '',
  '| Task | State |',
  '|---|---|',
  '| Welcome note | working |',
  '',
  '</mcwidget>',
].join('\n')

describe('markdown inside a widget tag', () => {
  it('renders the table as a table and the bold as bold, under the title', () => {
    const { getByTestId, container, queryByTestId } = render(<MarkdownRenderer content={MD_WIDGET} />)
    const card = getByTestId('markdown-widget')
    expect(card).toHaveAttribute('aria-label', 'Task board')
    expect(card.querySelector('table')).not.toBeNull()
    expect(card.querySelector('strong')?.textContent).toBe('Needs you:')
    expect(container.textContent).not.toContain('|---|')
    expect(queryByTestId('widget-frame')).toBeNull()
  })

  it('an HTML body still goes to the widget frame', () => {
    const html = '<mcwidget title="Card">\n<div style="color:red">Hi</div>\n</mcwidget>'
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={html} />)
    expect(getByTestId('widget-frame')).toHaveTextContent('Card')
    expect(queryByTestId('markdown-widget')).toBeNull()
  })
})
