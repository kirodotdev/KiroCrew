// @vitest-environment happy-dom
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { looksLikeMarkdown } from '../components/markdown/markdownSniff'

/**
 * #13152 — an untagged or generic-language fence (```, ```text, ```txt,
 * ```plaintext, ```plain) whose content is clearly markdown gets the same
 * Formatted | Raw card a ```markdown fence gets. "Clearly" means two or more
 * DISTINCT block-level signals, at least one of them a table or a URL link
 * (code never makes those); any real language is never sniffed.
 */

const fence = (lang: string, body: string) => '```' + lang + '\n' + body + '\n```'
const hasCard = (content: string, streaming = false) => {
  const { queryByText, unmount } = render(<MarkdownRenderer content={content} streaming={streaming} mdCardToggle />)
  const shown = queryByText('Formatted') !== null && queryByText('Raw') !== null
  unmount()
  return shown
}

const DOC = '# Report\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nSee [docs](https://example.com).'

describe('#13152 markdown card for untagged fences that hold markdown', () => {
  it('shows the card for an untagged heading + table document', () => {
    expect(hasCard(fence('', DOC))).toBe(true)
  })

  it.each(['text', 'txt', 'plaintext', 'plain', 'TEXT'])('shows the card for a generic ```%s fence', lang => {
    expect(hasCard(fence(lang, DOC))).toBe(true)
  })

  it('does not show the card for a single signal (just a list)', () => {
    expect(hasCard(fence('', '- one\n- two\n- three'))).toBe(false)
  })

  it('does not show the card for a shell transcript full of -f flags', () => {
    const shell = '$ tar -czf out.tgz src\n- removed: build/\n$ rm -f out.tgz\n  -f, --force   ignore missing files'
    expect(hasCard(fence('', shell))).toBe(false)
  })

  it('does not show the card for a diff with ---/+++ headers', () => {
    const diff = '--- a/x.txt\n+++ b/x.txt\n@@ -1,2 +1,2 @@\n-old\n+new\n same'
    expect(hasCard(fence('', diff))).toBe(false)
  })

  it.each([
    ['a Python comment + indexed call', '# dispatch the handler\ndef run(name, req):\n    return handlers[name](req)'],
    ['a JSDoc block + indexed call', '/**\n * Dispatch an event.\n */\nfunction fire(e) {\n  return listeners[e.type](e)\n}'],
    ['a diff with indented changed lines', '--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n def f(d, k):\n-    return d[k](1)\n+    return d[k](2)'],
    ['a C preprocessor line + indexed call', '# define N 3\nint main() { return fns[0](N); }'],
    ['unittest output', 'FAIL: test_x\n----------------------------------------------------------------------\n- see failures above'],
    ['a shell transcript with a comment', '# clean up\n$ rm -rf build\n- removed: build/'],
    ['YAML with a document marker', '---\nservices:\n  - web\n  - db'],
    ['YAML with a comment', '# config\nitems:\n  - a\n  - b'],
    ['an indexed call with a URL-ish target (link lookbehind guard)', '# notes\nx[a](#b)'],
    ['a bracket pair with a non-URL target (link target guard)', '# notes\n[text](req)'],
  ])('does not show the card for code: %s', (_name, body) => {
    expect(hasCard(fence('', body))).toBe(false)
  })

  it('shows the card for a heading + URL link document with no table', () => {
    expect(hasCard(fence('', '## Notes\n\n- item\n\nSee [the guide](./guide.md).'))).toBe(true)
  })

  it('sniffs in linear time: a newline run that makes a \\s-based pattern quadratic', () => {
    // A table row (strong) and no weak signal, so every weak pattern scans
    // the whole run without matching. `^\s*[-*+]\s+\S` with /m then
    // backtracks across every remaining newline from each line start: about
    // 20 s on 100k newlines. The one-line patterns finish in milliseconds,
    // well inside the 2 s timeout below.
    expect(looksLikeMarkdown('\n'.repeat(100_000) + '|---|---|')).toBe(false)
  }, 2000)

  it('never sniffs a real language: a ```python fence with markdown content stays code', () => {
    expect(hasCard(fence('python', DOC))).toBe(false)
  })

  it('keeps the complete-fence gate: a still-streaming untagged fence gets no card', () => {
    expect(hasCard('```\n' + DOC, true)).toBe(false)
  })
})
