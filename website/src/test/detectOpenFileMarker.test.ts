import { describe, it, expect } from 'vitest'

import { isOpenableMarkerPath, openFileMarkerPaths, OPEN_FILE_MARKER_MAX_CHIPS } from '../utils/detectOpenFileMarker'

const marker = (path: string) => `<!-- kirocrew:open-file path="${path}" -->`

describe('openFileMarkerPaths', () => {
  it('returns the absolute path a marker in the reply names', () => {
    expect(openFileMarkerPaths(`Wrote the note.\n\n${marker('/home/u/notes/today.md')}\n`)).toEqual(['/home/u/notes/today.md'])
  })

  it('finds a marker inline in a paragraph, on its own line, and in a blockquote', () => {
    expect(openFileMarkerPaths(`Wrote it ${marker('/a/inline.md')} just now.`)).toEqual(['/a/inline.md'])
    expect(openFileMarkerPaths(`Wrote it.\n${marker('/a/next-line.md')}`)).toEqual(['/a/next-line.md'])
    expect(openFileMarkerPaths(`> Wrote it.\n> ${marker('/a/quoted.md')}`)).toEqual(['/a/quoted.md'])
  })

  it('accepts Windows drive-qualified paths', () => {
    expect(openFileMarkerPaths(marker('C:\\Users\\u\\notes.md'))).toEqual(['C:\\Users\\u\\notes.md'])
    expect(openFileMarkerPaths(marker('D:/work/notes.md'))).toEqual(['D:/work/notes.md'])
  })

  it('tolerates whitespace inside the comment', () => {
    expect(openFileMarkerPaths('<!--   kirocrew:open-file   path="/a/b.md"   -->')).toEqual(['/a/b.md'])
  })

  it('returns nothing without a marker, and ignores the preview marker', () => {
    expect(openFileMarkerPaths('see /home/u/notes.md')).toEqual([])
    expect(openFileMarkerPaths('<!-- kirocrew:preview url="http://127.0.0.1:8080" -->')).toEqual([])
    expect(openFileMarkerPaths('')).toEqual([])
  })

  it('ignores a marker the reply shows as code, however the code is written', () => {
    const m = marker('/home/u/a.md')
    for (const [label, text] of [
      ['backtick fence', `Emit this:\n\n\`\`\`\n${m}\n\`\`\`\n`],
      ['tilde fence', `Emit this:\n\n~~~html\n${m}\n~~~`],
      ['inline code', `Use \`${m}\` to offer a file.`],
      ['fence in a blockquote', `> \`\`\`\n> ${m}\n> \`\`\``],
      ['indented code block', `Emit this:\n\n    ${m}\n`],
      ['fence indented in a list item', `- step one:\n\n      \`\`\`\n      ${m}\n      \`\`\``],
      // The renderer's fixCodeFences opens a fence glued to the sentence before it.
      ['fence glued to a sentence', `Emit this:\`\`\`\n${m}\n\`\`\``],
      ['tagged fence glued to a sentence', `Emit this:\`\`\`html\n${m}\n\`\`\``],
      // Lifted out of the prose by the block splitter and rendered in its own frame.
      ['widget body', `Here is the card:\n\n<mcwidget title="Notes">\n<div>${m}</div>\n</mcwidget>\n`],
    ]) {
      expect(openFileMarkerPaths(text), label).toEqual([])
    }
    // A real marker after the fenced example still counts.
    expect(openFileMarkerPaths(`\`\`\`\n${marker('/x.md')}\n\`\`\`\n\n${marker('/home/u/real.md')}`)).toEqual(['/home/u/real.md'])
  })

  it('returns every marked file once, in reply order', () => {
    expect(openFileMarkerPaths(`${marker('/a.md')} then ${marker('/b.md')} and ${marker('/a.md')}`)).toEqual(['/a.md', '/b.md'])
  })

  it('skips a marker naming a refused path and keeps the others', () => {
    expect(openFileMarkerPaths(`${marker('/good.md')} ${marker('relative.md')} ${marker('/also-good.md')}`))
      .toEqual(['/good.md', '/also-good.md'])
  })

  it('caps the chips one reply offers', () => {
    const many = Array.from({ length: OPEN_FILE_MARKER_MAX_CHIPS + 3 }, (_, i) => marker(`/n/${i}.md`)).join('\n\n')
    expect(openFileMarkerPaths(many)).toHaveLength(OPEN_FILE_MARKER_MAX_CHIPS)
  })
})

describe('isOpenableMarkerPath', () => {
  it('accepts the absolute paths a path chip accepts', () => {
    for (const good of ['/home/u/notes.md', '/Demo Workspace/Product Guide/notes.md', 'C:\\Users\\u\\notes.md', 'D:/work/notes.md']) {
      expect(isOpenableMarkerPath(good), good).toBe(true)
    }
  })

  it('refuses relative and ~ paths, which resolve against a directory the agent did not name', () => {
    for (const bad of ['notes.md', './notes.md', '../etc/passwd', 'sub/dir/notes.md', '~', '~/notes.md', '', 'C:notes.md']) {
      expect(isOpenableMarkerPath(bad), bad).toBe(false)
      expect(openFileMarkerPaths(marker(bad)), bad).toEqual([])
    }
  })

  it('refuses what a path chip refuses: UNC roots and command lines', () => {
    for (const bad of ['//host/share/x.md', '\\\\host\\share\\x.md', '/usr/bin/tool --force /tmp/x']) {
      expect(isOpenableMarkerPath(bad), bad).toBe(false)
    }
  })
})
