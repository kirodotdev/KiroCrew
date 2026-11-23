import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ApprovalInputText } from './ApprovalInputText'

const kindsOf = (root: HTMLElement) => Array.from(root.querySelectorAll('[data-diff-line]')).map(el => el.getAttribute('data-diff-line'))

describe('ApprovalInputText', () => {
  it('leaves a non-diff command as the exact plain text', () => {
    const { container } = render(<pre><ApprovalInputText text={'rm -rf *cache*\n+x'} /></pre>)
    expect(container.textContent).toBe('rm -rf *cache*\n+x')
    expect(container.querySelector('[data-diff-line]')).toBeNull()
  })

  it('keeps a command that precedes a patch visible, byte for byte', () => {
    const text = "git apply <<'EOF'\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\nEOF"
    const { container } = render(<pre><ApprovalInputText text={text} /></pre>)
    expect(container.textContent).toBe(text)
    expect(kindsOf(container)).toEqual(['meta', 'old-header', 'new-header', 'hunk', 'del', 'add', 'ctx'])
  })

  it('styles a removed "-- " line inside a hunk as removed, and the next file header as a header', () => {
    const text = [
      'diff --git a/x.sql b/x.sql', '--- a/x.sql', '+++ b/x.sql', '@@ -1,2 +1,1 @@',
      '--- a SQL comment', ' select 1;',
      '--- a/y.txt', '+++ b/y.txt', '@@ -1 +1 @@', '-a', '+b', '\\ No newline at end of file',
    ].join('\n')
    const { container } = render(<pre><ApprovalInputText text={text} /></pre>)
    expect(container.textContent).toBe(text)
    expect(kindsOf(container)).toEqual([
      'file', 'old-header', 'new-header', 'hunk', 'del', 'ctx',
      'old-header', 'new-header', 'hunk', 'del', 'add', 'marker',
    ])
  })
})
