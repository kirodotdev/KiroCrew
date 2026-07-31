import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  buildTree, countDiagnostics, countWords, flattenTree, gitBranchLabel, isArtifact,
  LATEX_LANGUAGE_ID, loadLastProject, loadSlot, monacoLanguage, pruneSlots, saveLastProject,
  saveSlot, SLOT_KEY_PREFIX, sourceFiles, texFiles,
} from '../apps/papyrus/lib'
import type { Diagnostic } from '../apps/papyrus/api'

// Papyrus's pure helpers. These carry the behaviour that is otherwise only
// observable by staring at the rendered tree — the file-tree shape, the
// artifact filter, the LaTeX word count, and the localStorage bookkeeping that
// binds a paper to its co-author chat session.

describe('artifact filtering', () => {
  it.each([
    'main.aux', 'main.log', 'main.bbl', 'main.blg', 'main.out', 'main.toc',
    'main.pdf', 'main.synctex.gz', 'main.fls', 'main.fdb_latexmk',
  ])('treats %s as a build artifact', (name) => {
    expect(isArtifact(name)).toBe(true)
  })

  it.each(['main.tex', 'references.bib', 'acl_natbib.bst', 'acl.sty', 'figures/plot.png'])(
    'leaves %s as source',
    (name) => {
      expect(isArtifact(name)).toBe(false)
    },
  )

  it('is case-insensitive', () => {
    // A cloned repo can carry `MAIN.AUX` on a case-insensitive filesystem.
    expect(isArtifact('MAIN.AUX')).toBe(true)
  })

  it('filters a flat listing down to source', () => {
    expect(sourceFiles(['main.tex', 'main.aux', 'main.pdf', 'refs.bib'])).toEqual([
      'main.tex', 'refs.bib',
    ])
  })

  it('narrows to .tex for the main-document picker', () => {
    expect(texFiles(['main.tex', 'refs.bib', 'sections/intro.tex'])).toEqual([
      'main.tex', 'sections/intro.tex',
    ])
  })
})

describe('monacoLanguage', () => {
  it('routes .tex to the app-registered LaTeX grammar', () => {
    // Monaco bundles no TeX grammar, so Papyrus registers its own; claiming a
    // built-in id here would silently mis-highlight every backslash.
    expect(monacoLanguage('main.tex')).toBe(LATEX_LANGUAGE_ID)
    expect(monacoLanguage('acl.sty')).toBe(LATEX_LANGUAGE_ID)
    expect(monacoLanguage('references.bib')).toBe(LATEX_LANGUAGE_ID)
  })

  it('uses a bundled grammar where one exists', () => {
    expect(monacoLanguage('README.md')).toBe('markdown')
    expect(monacoLanguage('build.sh')).toBe('shell')
    expect(monacoLanguage('script.py')).toBe('python')
  })

  it('falls back to plaintext for an unknown extension', () => {
    expect(monacoLanguage('notes.txt')).toBe('plaintext')
    expect(monacoLanguage('LICENSE')).toBe('plaintext')
  })
})

describe('buildTree', () => {
  it('nests paths into folders', () => {
    const tree = buildTree(['main.tex', 'sections/intro.tex', 'sections/method.tex'])
    expect(tree.map(n => n.name)).toEqual(['sections', 'main.tex'])
    const sections = tree[0]
    expect(sections.isFolder).toBe(true)
    expect(sections.children.map(n => n.path)).toEqual([
      'sections/intro.tex', 'sections/method.tex',
    ])
  })

  it('sorts folders before files, then alphabetically', () => {
    const tree = buildTree(['zeta.tex', 'alpha.tex', 'beta/x.tex', 'aardvark/y.tex'])
    expect(tree.map(n => n.name)).toEqual(['aardvark', 'beta', 'alpha.tex', 'zeta.tex'])
  })

  it('is independent of the input order', () => {
    const forward = buildTree(['a/x.tex', 'b/y.tex', 'c.tex'])
    const reverse = buildTree(['c.tex', 'b/y.tex', 'a/x.tex'])
    expect(JSON.stringify(forward)).toBe(JSON.stringify(reverse))
  })

  it('handles arbitrary depth', () => {
    const tree = buildTree(['a/b/c/deep.tex'])
    expect(tree[0].children[0].children[0].children[0].path).toBe('a/b/c/deep.tex')
  })

  it('keeps a file and a folder that share a name segment distinct', () => {
    const tree = buildTree(['figures.tex', 'figures/plot.tex'])
    expect(tree.map(n => `${n.name}:${n.isFolder}`)).toEqual(['figures:true', 'figures.tex:false'])
  })

  it('returns nothing for an empty listing', () => {
    expect(buildTree([])).toEqual([])
  })
})

describe('flattenTree', () => {
  const tree = buildTree(['main.tex', 'sections/intro.tex', 'sections/method.tex'])

  it('emits every row with its depth when nothing is collapsed', () => {
    const rows = flattenTree(tree, new Set())
    expect(rows.map(r => [r.node.name, r.depth])).toEqual([
      ['sections', 0], ['intro.tex', 1], ['method.tex', 1], ['main.tex', 0],
    ])
  })

  it('hides the children of a collapsed folder but keeps the folder', () => {
    const rows = flattenTree(tree, new Set(['sections']))
    expect(rows.map(r => r.node.name)).toEqual(['sections', 'main.tex'])
  })
})

describe('countDiagnostics', () => {
  const make = (level: Diagnostic['level']): Diagnostic =>
    ({ level, message: 'm', line: 1, file: null })

  it('tallies each level separately', () => {
    const counts = countDiagnostics([
      make('error'), make('error'), make('warning'), make('typesetting'),
    ])
    expect(counts).toEqual({ errors: 2, warnings: 1, typesetting: 1 })
  })

  it('is zero for an empty list', () => {
    expect(countDiagnostics([])).toEqual({ errors: 0, warnings: 0, typesetting: 0 })
  })
})

describe('countWords', () => {
  it('counts prose', () => {
    expect(countWords('The quick brown fox')).toBe(4)
  })

  it('ignores commands', () => {
    // The count should track what a reader counts, not the size of the markup.
    expect(countWords('\\textbf{}\nHello there world')).toBe(3)
  })

  it('drops a command token whole, argument included', () => {
    // `\section{Introduction}` is ONE whitespace-delimited token, so the heading
    // text goes with the command. Splitting it out would need brace matching, and
    // a heading is a handful of words against a body of thousands — so this is a
    // documented, deliberate approximation rather than an oversight.
    expect(countWords('\\section{Introduction}\nHello there')).toBe(2)
  })

  it('ignores comment lines', () => {
    expect(countWords('real words here\n% a commented note about things')).toBe(3)
  })

  it('counts an escaped percent as prose, not a comment', () => {
    // `\%` is a literal percent sign — extremely common in a results table, and
    // treating it as a comment marker would silently drop the rest of the line.
    expect(countWords('gains of 95\\% were observed')).toBe(4)
  })

  it('ignores inline and display math', () => {
    expect(countWords('before $x + y = z$ after')).toBe(2)
    expect(countWords('before $$\\sum_{i=1}^{N} x_i$$ after')).toBe(2)
  })

  it('ignores equation environments', () => {
    const source = 'Intro text.\n\\begin{equation}\n  E = mc^2\n\\end{equation}\nOutro text.'
    expect(countWords(source)).toBe(4)
  })

  it('ignores starred equation environments', () => {
    const source = 'one\n\\begin{align*}\na = b\n\\end{align*}\ntwo'
    expect(countWords(source)).toBe(2)
  })

  it('ignores pure punctuation and digits', () => {
    expect(countWords('--- 42 !!! word')).toBe(1)
  })

  it('is zero for an empty document', () => {
    expect(countWords('')).toBe(0)
  })
})

describe('gitBranchLabel', () => {
  it('is empty for a non-repo', () => {
    expect(gitBranchLabel({ is_git: false })).toBe('')
    expect(gitBranchLabel(undefined)).toBe('')
  })

  it('shows the branch', () => {
    expect(gitBranchLabel({ is_git: true, branch: 'main' })).toBe('main')
  })

  it('marks a dirty tree', () => {
    expect(gitBranchLabel({ is_git: true, branch: 'main', dirty: true })).toBe('main*')
  })
})

describe('project + slot persistence', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('round-trips the last-open project', () => {
    saveLastProject('my-paper')
    expect(loadLastProject()).toBe('my-paper')
  })

  it('clears the last-open project', () => {
    saveLastProject('my-paper')
    saveLastProject(null)
    expect(loadLastProject()).toBeNull()
  })

  it('round-trips a co-author slot per project', () => {
    saveSlot('paper-a', 'slot-1')
    saveSlot('paper-b', 'slot-2')
    expect(loadSlot('paper-a')).toBe('slot-1')
    expect(loadSlot('paper-b')).toBe('slot-2')
  })

  it('has no slot for an unknown project', () => {
    expect(loadSlot('never-seen')).toBeNull()
  })

  it('prunes slots whose project is gone', () => {
    // A name reused after a delete would otherwise resurrect the OLD paper's
    // conversation, which reads as the agent inventing context.
    saveSlot('kept', 'slot-1')
    saveSlot('deleted', 'slot-2')
    pruneSlots(['kept'])
    expect(loadSlot('kept')).toBe('slot-1')
    expect(loadSlot('deleted')).toBeNull()
  })

  it('leaves unrelated keys alone when pruning', () => {
    localStorage.setItem('kc:unrelated', 'keep-me')
    saveSlot('gone', 'slot')
    pruneSlots([])
    expect(localStorage.getItem('kc:unrelated')).toBe('keep-me')
  })

  it('namespaces slot keys', () => {
    saveSlot('paper', 'slot-1')
    expect(localStorage.getItem(`${SLOT_KEY_PREFIX}paper`)).toBe('slot-1')
  })

  it('survives storage being unavailable', () => {
    // Private-mode Safari throws on setItem; losing the restore is acceptable,
    // taking the page down with it is not.
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    expect(() => saveLastProject('x')).not.toThrow()
    expect(() => saveSlot('p', 's')).not.toThrow()
    setItem.mockRestore()
  })

  it('survives a read failure', () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError')
    })
    expect(loadLastProject()).toBeNull()
    expect(loadSlot('p')).toBeNull()
    getItem.mockRestore()
  })
})
