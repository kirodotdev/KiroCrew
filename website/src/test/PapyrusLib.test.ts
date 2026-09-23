import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  buildTree, countDiagnostics, countWords, DEFAULT_PDF_WIDTH, defaultPdfWidth,
  flattenTree, gitBranchLabel, isArtifact,
  loadLastProject, loadSlot, MAX_PDF_WIDTH, maxPdfWidth, MIN_PDF_WIDTH, pruneSlots,
  saveLastProject, saveSlot, SLOT_KEY_PREFIX, sourceFiles, texFiles,
  DEFAULT_CHAT_WIDTH, MAX_CHAT_WIDTH, maxChatWidth, MIN_CHAT_WIDTH,
  DEFAULT_TREE_WIDTH, MIN_EDITOR_WIDTH,
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

describe('maxPdfWidth', () => {
  // The preview's ceiling is a share of the window because the editor has no
  // grip of its own: it absorbs whatever the preview leaves, so a fixed cap
  // decides how small the author is allowed to make the editor, and that
  // allowance has to scale with the screen.
  it.each([
    [1280, 640],
    [1440, 720],
    [1920, 960],
    [2560, 1280],
    [3440, 1720],
  ])('gives half of a %ipx window to the preview', (viewport, expected) => {
    expect(maxPdfWidth(viewport)).toBe(expected)
  })

  it('reproduces the reach the 50% split had before the column was resizable', () => {
    // The regression this replaces: a flat 900 took 380px of reach away at 2560.
    expect(maxPdfWidth(2560)).toBeGreaterThan(MAX_PDF_WIDTH)
  })

  it('caps below the old flat ceiling on a narrow window, so a drag cannot crush the editor', () => {
    expect(maxPdfWidth(1280)).toBeLessThan(MAX_PDF_WIDTH)
  })

  it('never returns less than the column can legally be', () => {
    // A 320px phone halves to 160, below MIN_PDF_WIDTH; an empty range would make
    // the hook's clamp resolve min above max.
    expect(maxPdfWidth(320)).toBe(MIN_PDF_WIDTH)
    expect(maxPdfWidth(1)).toBe(MIN_PDF_WIDTH)
  })

  it('falls back to the flat ceiling when the window cannot be measured', () => {
    // A server render or a test renderer with no layout reports 0.
    expect(maxPdfWidth(0)).toBe(MAX_PDF_WIDTH)
  })
})

describe('defaultPdfWidth', () => {
  // `loadColumnWidth` returns its fallback UNCHANGED when the stored width is
  // unusable, so the fallback has to be legal before it gets there — otherwise
  // the very first render sits outside the range the grip announces.
  it('leaves the chosen default alone once the window is wide enough to hold it', () => {
    expect(defaultPdfWidth(1040)).toBe(DEFAULT_PDF_WIDTH)
    expect(defaultPdfWidth(2560)).toBe(DEFAULT_PDF_WIDTH)
  })

  it.each([
    [800, 400],
    [900, 450],
    [1000, 500],
  ])('gives way to the ceiling on a %ipx window', (viewport, expected) => {
    expect(defaultPdfWidth(viewport)).toBe(expected)
  })

  it('never exceeds the ceiling the grip advertises', () => {
    // The defect this closes: a 769-1039px window advertised aria-valuemax
    // 385-520 while rendering 520, so the reported position was out of range
    // until the first drag pulled it back in.
    for (let viewport = 700; viewport <= 1200; viewport += 1) {
      expect(defaultPdfWidth(viewport)).toBeLessThanOrEqual(maxPdfWidth(viewport))
    }
  })

  it('stays a legal column width on a viewport too small to halve', () => {
    expect(defaultPdfWidth(320)).toBe(MIN_PDF_WIDTH)
  })

  it('keeps the chosen default when the window cannot be measured', () => {
    expect(defaultPdfWidth(0)).toBe(DEFAULT_PDF_WIDTH)
  })
})

describe('maxChatWidth', () => {
  // The editor owns no grip: it renders what the other three columns leave. So
  // its floor can only be enforced by capping the column that yields, and the
  // co-author panel is it.
  it('leaves the editor its floor on a laptop with the other columns at their defaults', () => {
    const ceiling = maxChatWidth(1280, DEFAULT_TREE_WIDTH, DEFAULT_PDF_WIDTH)
    expect(ceiling).toBe(304)
    expect(1280 - DEFAULT_TREE_WIDTH - DEFAULT_PDF_WIDTH - ceiling)
      .toBeGreaterThanOrEqual(MIN_EDITOR_WIDTH)
  })

  it('is what the unclamped default would have taken away', () => {
    // The defect: 176 + 520 + 420 = 1116 of a 1280px window, leaving the pane
    // carrying the text being written at 164px — and `chat-open` persistence
    // made that the layout the author returns to, not a transient squeeze.
    expect(1280 - DEFAULT_TREE_WIDTH - DEFAULT_PDF_WIDTH - DEFAULT_CHAT_WIDTH)
      .toBeLessThan(MIN_EDITOR_WIDTH)
    expect(maxChatWidth(1280, DEFAULT_TREE_WIDTH, DEFAULT_PDF_WIDTH))
      .toBeLessThan(DEFAULT_CHAT_WIDTH)
  })

  it('holds the floor across every desktop width, or is already pinned at its minimum', () => {
    // The panel can only pay out of what it has above 280px, so below ~1256px
    // (176 tree + 520 preview + 280 panel + 280 editor) the floor is no longer
    // purchasable and the editor takes the remainder. Both regimes are asserted
    // here because a ceiling that dipped under MIN_CHAT_WIDTH to chase the floor
    // would make the hook's clamp resolve max below min.
    let pinned = 0
    let honoured = 0
    for (let viewport = 800; viewport <= 3440; viewport += 1) {
      const pdf = Math.min(DEFAULT_PDF_WIDTH, Math.round(viewport / 2))
      const ceiling = maxChatWidth(viewport, DEFAULT_TREE_WIDTH, pdf)
      expect(ceiling, `ceiling under the column minimum on a ${viewport}px window`)
        .toBeGreaterThanOrEqual(MIN_CHAT_WIDTH)
      const editor = viewport - DEFAULT_TREE_WIDTH - pdf - ceiling
      if (ceiling === MIN_CHAT_WIDTH) pinned++
      else {
        honoured++
        expect(editor, `editor crushed on a ${viewport}px window`)
          .toBeGreaterThanOrEqual(MIN_EDITOR_WIDTH)
      }
    }
    // Neither branch may be vacuous, or the loop proves nothing about the other.
    expect(pinned).toBeGreaterThan(0)
    expect(honoured).toBeGreaterThan(0)
  })

  it('starts honouring the floor at the width where the panel can afford it', () => {
    // The crossover, pinned down so a change to any of the four numbers shows up
    // as a moved boundary rather than silently.
    expect(maxChatWidth(1255, DEFAULT_TREE_WIDTH, DEFAULT_PDF_WIDTH)).toBe(MIN_CHAT_WIDTH)
    expect(maxChatWidth(1256, DEFAULT_TREE_WIDTH, DEFAULT_PDF_WIDTH)).toBe(MIN_CHAT_WIDTH)
    expect(maxChatWidth(1257, DEFAULT_TREE_WIDTH, DEFAULT_PDF_WIDTH)).toBe(281)
  })

  it('stops yielding at its own minimum rather than resolving below it', () => {
    // Under ~1040px the panel alone cannot buy the floor back, so it stops at
    // 280 and the editor takes what is left. A ceiling under the minimum would
    // make the hook's clamp resolve max below min, and the grip would announce
    // an empty range.
    expect(maxChatWidth(1024, DEFAULT_TREE_WIDTH, 512)).toBe(MIN_CHAT_WIDTH)
    expect(maxChatWidth(600, DEFAULT_TREE_WIDTH, 300)).toBe(MIN_CHAT_WIDTH)
  })

  it('never caps a window wide enough to hold every column at full width', () => {
    expect(maxChatWidth(2560, DEFAULT_TREE_WIDTH, 1280)).toBe(MAX_CHAT_WIDTH)
    expect(maxChatWidth(3440, DEFAULT_TREE_WIDTH, 1720)).toBe(MAX_CHAT_WIDTH)
  })

  it('hands the room straight over when a neighbour gives some back', () => {
    // Collapsing the tree to its 28px rail, or dragging the preview in, is room
    // the panel may take — which is why this is a ceiling on the width rendered
    // and not a discard of the width stored.
    const withTree = maxChatWidth(1280, DEFAULT_TREE_WIDTH, DEFAULT_PDF_WIDTH)
    expect(maxChatWidth(1280, 28, DEFAULT_PDF_WIDTH)).toBeGreaterThan(withTree)
    expect(maxChatWidth(1280, DEFAULT_TREE_WIDTH, 280)).toBeGreaterThan(withTree)
  })

  it('falls back to the flat ceiling when the window cannot be measured', () => {
    expect(maxChatWidth(0, DEFAULT_TREE_WIDTH, DEFAULT_PDF_WIDTH)).toBe(MAX_CHAT_WIDTH)
  })
})
