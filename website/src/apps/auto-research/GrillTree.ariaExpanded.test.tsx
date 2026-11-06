/**
 * The grill tree's per-node collapse chevron shows and hides that node's
 * children, so it must tell assistive tech whether the branch is open.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import '../../i18n'
import { i18nT } from '../../i18n/t'
import GrillTree from './GrillTree'
import type { GrillNode } from './grillTreeModel'

const node = (id: string, parent: string | null, text: string): GrillNode => ({
  id,
  parent,
  kind: 'research',
  text,
  recommended: '',
  answer: '',
  origin: 'grill',
  status: 'promoted',
})

const tree: GrillNode[] = [
  node('root', null, 'Top question'),
  node('kid', 'root', 'Child question'),
]

afterEach(cleanup)

describe('GrillTree collapse chevron', () => {
  it('reports aria-expanded as the branch opens and closes', () => {
    render(<GrillTree tree={tree} dispatch={vi.fn()} onExpand={vi.fn(async () => undefined)} />)

    const collapse = screen.getByRole('button', { name: i18nT('apps.autoResearch.grillTree.collapse') })
    expect(collapse.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByDisplayValue('Child question')).toBeTruthy()

    fireEvent.click(collapse)
    const expand = screen.getByRole('button', { name: i18nT('apps.autoResearch.grillTree.expand_2') })
    expect(expand).toBe(collapse)
    expect(expand.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByDisplayValue('Child question')).toBeNull()
  })

  it('draws no chevron for a leaf node', () => {
    render(<GrillTree tree={[node('solo', null, 'Lonely')]} dispatch={vi.fn()} onExpand={vi.fn(async () => undefined)} />)
    expect(screen.queryByRole('button', { name: i18nT('apps.autoResearch.grillTree.collapse') })).toBeNull()
    expect(document.querySelector('[aria-expanded]')).toBeNull()
  })
})
