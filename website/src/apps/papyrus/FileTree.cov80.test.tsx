/**
 * FileTree — the Papyrus source-file tree.
 *
 * Covers the parts that are the component's own logic rather than `lib.ts`'s:
 * build artifacts never reaching a row, the per-extension glyph branches, the
 * collapse toggle, the main-file badge suppressing the delete affordance, and
 * the empty state.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import FileTree, { type FileTreeProps } from './FileTree'
import { i18nT } from '../../i18n/t'

const FILES = [
  'main.tex',
  'refs.bib',
  'zzqstyle.sty',
  'zzqclass.cls',
  'figs/plot.png',
  'sections/intro.tex',
  'main.aux', // build artifact — must never render
  'main.pdf', // build artifact — must never render
]

function setup(props: Partial<FileTreeProps> = {}) {
  const handlers = {
    onOpenFile: vi.fn(),
    onCreateFile: vi.fn(),
    onDeleteFile: vi.fn(),
  }
  const utils = render(
    <FileTree
      files={FILES}
      currentFile="main.tex"
      mainFile="main.tex"
      {...handlers}
      {...props}
    />,
  )
  return { ...utils, ...handlers }
}

describe('FileTree rows', () => {
  it('renders source files and hides build artifacts', () => {
    setup()
    expect(screen.getByText('main.tex')).toBeInTheDocument()
    expect(screen.getByText('refs.bib')).toBeInTheDocument()
    expect(screen.queryByText('main.aux')).not.toBeInTheDocument()
    expect(screen.queryByText('main.pdf')).not.toBeInTheDocument()
  })

  it('renders a folder row per directory, expanded by default', () => {
    setup()
    const folders = screen.getAllByRole('button', { expanded: true })
    expect(folders.map((f) => f.getAttribute('title')).sort()).toEqual(['figs', 'sections'])
    expect(screen.getByText('intro.tex')).toBeInTheDocument()
  })

  it('marks the open file with aria-current', () => {
    setup({ currentFile: 'refs.bib' })
    const open = screen.getByTitle('refs.bib')
    expect(open).toHaveAttribute('aria-current', 'true')
    expect(screen.getByTitle('main.tex')).not.toHaveAttribute('aria-current')
  })

  it('shows the empty state when every file is a build artifact', () => {
    setup({ files: ['main.aux', 'main.log'] })
    expect(screen.queryByTitle('main.aux')).not.toBeInTheDocument()
    expect(screen.getByTestId('papyrus-file-tree').textContent).toContain(
      i18nT('apps.papyrus.fileTree.no_source_files'),
    )
  })
})

describe('FileTree collapse', () => {
  it('collapses a folder on click and restores it on a second click', () => {
    setup()
    const folder = screen.getByTitle('sections')
    fireEvent.click(folder)
    expect(screen.getByTitle('sections')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('intro.tex')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTitle('sections'))
    expect(screen.getByTitle('sections')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('intro.tex')).toBeInTheDocument()
  })

  it('collapses only the clicked folder', () => {
    setup()
    fireEvent.click(screen.getByTitle('sections'))
    expect(screen.getByTitle('figs')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('plot.png')).toBeInTheDocument()
  })
})

describe('FileTree actions', () => {
  it('opens a file by its full path, not its display name', () => {
    const { onOpenFile } = setup()
    fireEvent.click(screen.getByTitle('sections/intro.tex'))
    expect(onOpenFile).toHaveBeenCalledWith('sections/intro.tex')
  })

  it('offers no delete affordance for the main document', () => {
    setup({ mainFile: 'main.tex' })
    expect(
      screen.queryByRole('button', {
        name: i18nT('apps.papyrus.fileTree.delete_file', { file: 'main.tex' }),
      }),
    ).not.toBeInTheDocument()
    expect(screen.getByTitle('main.tex').textContent).toContain(
      i18nT('apps.papyrus.fileTree.main'),
    )
  })

  it('deletes a non-main file by path', () => {
    const { onDeleteFile } = setup()
    fireEvent.click(
      screen.getByRole('button', {
        name: i18nT('apps.papyrus.fileTree.delete_file', { file: 'refs.bib' }),
      }),
    )
    expect(onDeleteFile).toHaveBeenCalledWith('refs.bib')
  })

  it('creates a file from the header button', () => {
    const { onCreateFile } = setup()
    fireEvent.click(
      screen.getByRole('button', { name: i18nT('apps.papyrus.fileTree.new_file') }),
    )
    expect(onCreateFile).toHaveBeenCalledTimes(1)
  })
})
