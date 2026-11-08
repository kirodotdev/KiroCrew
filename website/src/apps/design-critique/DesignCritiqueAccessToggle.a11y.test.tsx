/**
 * The blocked card's "Fix my access" button shows and hides the access steps,
 * so it must say whether they are open and which region it opens.
 */
import { createRef } from 'react'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import '../../i18n/all'
import Composer from './Composer'
import type { Blocked } from './types'

const BLOCKED: Blocked = {
  say: 'Cannot reach the repo',
  hint: 'Sign in first',
  fix: 'retry',
  auth: { lead: 'Run these:', cmds: ['gh auth login'], tail: 'Then try again.' },
} as Blocked

function mount(showAuth: boolean) {
  return render(
    <Composer
      staged={[]}
      refText=""
      dragging={false}
      blocked={BLOCKED}
      showAuth={showAuth}
      busy={false}
      err=""
      inputRef={createRef<HTMLInputElement>()}
      onPick={vi.fn()} onDrop={vi.fn()} onDragOver={vi.fn()} onDragLeave={vi.fn()} pickFile={vi.fn()}
      dropStaged={vi.fn()} moveStaged={vi.fn()} clearStaged={vi.fn()} start={vi.fn()} setRefText={vi.fn()}
      setBlocked={vi.fn()} setShowAuth={vi.fn()} onTryAgain={vi.fn()}
    />,
  )
}

describe('design critique access-steps toggle', () => {
  it('reports collapsed while the steps are hidden', () => {
    mount(false)
    const toggle = screen.getByRole('button', { name: 'Fix my access' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveAttribute('aria-controls', 'dc-access-steps')
  })

  it('reports expanded and points at the steps it opened', () => {
    const { container } = mount(true)
    const toggle = screen.getByRole('button', { name: 'Hide access steps' })
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const region = container.querySelector(`#${toggle.getAttribute('aria-controls')}`)
    expect(region).not.toBeNull()
    expect(region).toHaveTextContent('gh auth login')
  })
})
