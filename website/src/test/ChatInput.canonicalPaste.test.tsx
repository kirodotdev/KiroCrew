/**
 * `ChatInput` canonicalises the host's value + paste blocks BEFORE it picks a
 * composer, so the textarea path — touch devices (`lexicalComposer={!touchDevice}`
 * at the hosts) and the chunk-load fallback — is held to the same contract as the
 * Lexical composer: every marker names its own block, no two blocks share a seq.
 * A persisted draft can violate both; a marker resolves by seq alone, so without
 * this `expandAll` sends one block for both occurrences and the textarea's
 * id-keyed pill removal drops a twin.
 */
import { describe, it, expect, vi } from 'vitest'
import { useState } from 'react'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { expandAll, formatToken, type PasteBlock } from '../utils/pasteTokens'

const orig: PasteBlock = { id: 'paste-1', seq: 1, lines: 4, content: 'alpha\nbeta\ngamma\ndelta' }
// The state a draft persisted before the seq invariant can carry: two pills that
// shared seq 1 AND id (both minted from one block) and were then edited apart.
const editedTwin: PasteBlock = { id: 'paste-1', seq: 1, lines: 1, content: 'edited' }

function Host({ lexical, initial, initialBlocks }: { lexical: boolean; initial: string; initialBlocks: PasteBlock[] }) {
  const [value, setValue] = useState(initial)
  const [blocks, setBlocks] = useState(initialBlocks)
  return (
    <>
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={vi.fn()}
        pasteBlocks={blocks}
        onPasteBlocksChange={setBlocks}
        lexicalComposer={lexical}
      />
      <output data-testid="value">{value}</output>
      <output data-testid="blocks">{JSON.stringify(blocks)}</output>
    </>
  )
}

describe('ChatInput — canonical paste pair for BOTH composers', () => {
  it('textarea path: a draft with two divergent same-seq/same-id records is split, and the host receives the rewritten pair', async () => {
    const divergent = `x${formatToken(orig)}y${formatToken(editedTwin)}z`
    renderWithProviders(<Host lexical={false} initial={divergent} initialBlocks={[orig, editedTwin]} />)
    // The classic textarea is what mounted.
    expect(screen.getByRole('textbox').tagName).toBe('TEXTAREA')

    const second = { ...editedTwin, seq: 2 }
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(`x${formatToken(orig)}y${formatToken(second)}z`))
    const blocks = JSON.parse(screen.getByTestId('blocks').textContent!) as PasteBlock[]
    expect(blocks).toHaveLength(2)
    expect(blocks[0]).toEqual(orig)
    expect(blocks[1]).toEqual(expect.objectContaining({ seq: 2, lines: 1, content: 'edited' }))
    // Same id twins must not survive: the textarea removes a pill by id.
    expect(blocks[1].id).not.toBe(orig.id)
    // What the host would send: both pastes, each under its own marker.
    expect(expandAll(screen.getByTestId('value').textContent!, blocks)).toBe(`x${orig.content}yeditedz`)
    // The textarea shows the canonical value too.
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe(`x${formatToken(orig)}y${formatToken(second)}z`)
  })

  it('textarea path: distinct seqs that share one id are given unique ids (the ✕ removes pills by id)', async () => {
    const reseqButSameId: PasteBlock = { id: 'paste-1', seq: 2, lines: 1, content: 'edited' }
    const draft = `x${formatToken(orig)}y${formatToken(reseqButSameId)}z`
    renderWithProviders(<Host lexical={false} initial={draft} initialBlocks={[orig, reseqButSameId]} />)
    expect(screen.getByRole('textbox').tagName).toBe('TEXTAREA')
    await waitFor(() => {
      const blocks = JSON.parse(screen.getByTestId('blocks').textContent!) as PasteBlock[]
      expect(new Set(blocks.map(b => b.id)).size).toBe(2)
    })
    // Text and seqs are untouched — only the second record's id changed.
    expect(screen.getByTestId('value').textContent).toBe(draft)
    const blocks = JSON.parse(screen.getByTestId('blocks').textContent!) as PasteBlock[]
    expect(blocks.map(b => b.seq)).toEqual([1, 2])
    expect(blocks[0]).toEqual(orig)
    expect(expandAll(draft, blocks)).toBe(`x${orig.content}yeditedz`)
  })

  it('textarea path: a clean pair is left alone — no onChange / onPasteBlocksChange echo', async () => {
    const onChange = vi.fn()
    const onPasteBlocksChange = vi.fn()
    const clean = `x${formatToken(orig)}y`
    renderWithProviders(
      <ChatInput value={clean} onChange={onChange} onSend={vi.fn()} pasteBlocks={[orig]} onPasteBlocksChange={onPasteBlocksChange} lexicalComposer={false} />,
    )
    expect(screen.getByRole('textbox').tagName).toBe('TEXTAREA')
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(onChange).not.toHaveBeenCalled()
    expect(onPasteBlocksChange).not.toHaveBeenCalled()
  })

  it('lexical path: the same draft is split before it reaches the tree', async () => {
    const divergent = `x${formatToken(orig)}y${formatToken(editedTwin)}z`
    renderWithProviders(<Host lexical initial={divergent} initialBlocks={[orig, editedTwin]} />)
    const second = { ...editedTwin, seq: 2 }
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe(`x${formatToken(orig)}y${formatToken(second)}z`))
    await waitFor(() => expect(screen.getByTestId('paste-token-2')).toBeInTheDocument())
    const blocks = JSON.parse(screen.getByTestId('blocks').textContent!) as PasteBlock[]
    expect(blocks.map(b => b.seq)).toEqual([1, 2])
    expect(new Set(blocks.map(b => b.id)).size).toBe(2)
  })
})
