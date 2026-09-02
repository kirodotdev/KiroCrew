import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import ChatInput from '../components/ChatInput'
import { renderWithProviders } from './helpers'

const INDEX_CSS = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf-8')

describe('mobile composer typography', () => {
  it('floors the textarea and paste mirror at 16px on coarse pointers', () => {
    const { container } = renderWithProviders(
      <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />,
    )

    expect(screen.getByLabelText('Message input')).toHaveAttribute('data-composer-typo')
    expect(container.querySelector('[aria-hidden][data-composer-typo]')).not.toBeNull()
    // The attribute is doubled to raise specificity to (0,2,0) so the rule outranks
    // Tailwind's `.text-sm` (0,1,0) NO MATTER which stylesheet the bundler emits
    // last. A bare `[data-composer-typo]` ties at (0,1,0) and would win only while
    // index.css happens to follow the utilities -- an import-order or build-pipeline
    // change would then make the fix inert on device while every assertion here
    // still passed. Doubling stays element-agnostic on purpose: the hook is on a
    // <textarea> AND on the paste mirror's <div>, so element-qualifying it
    // (`textarea[data-composer-typo]`) would raise specificity but silently drop
    // the mirror, whose font metrics must track the field's.
    expect(INDEX_CSS).toMatch(
      /@media\s*\(pointer:\s*coarse\)\s*\{[\s\S]*?\[data-composer-typo\]\[data-composer-typo\]\s*\{\s*font-size:\s*16px;\s*\}/,
    )
  })

  // jsdom does not evaluate media queries, so this matches the rule's text rather
  // than its effect: it guards the pairing (a 16px field must keep a smaller
  // placeholder) against one half being edited away, and cannot show the cascade
  // resolves. That is covered by the device capture on the pull request.
  it('holds the placeholder at the desktop size so the hint stays on one line', () => {
    expect(INDEX_CSS).toMatch(
      /\[data-composer-typo\]::placeholder\s*\{\s*font-size:\s*0\.875rem;\s*\}/,
    )
  })
})
