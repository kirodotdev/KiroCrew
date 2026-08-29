/**
 * Unit tests for ``buildReviewChatMessage`` -- the writing-review-reviewer
 * agent handoff builder.
 *
 * Two behaviours pinned here:
 *
 * * ``.docx`` reviews MUST inline the extracted prose (headings,
 *   paragraphs, [VISUAL:] placeholders) directly into the chat message.
 *   The agent cannot ``fs_read`` a docx (it's a ZIP archive of XML) --
 *   ``fs_read`` returns undecodable ZIP bytes. Inlining the extracted
 *   prose is the only way the agent can reason about the document's
 *   content in the discussion.
 *
 * * ``.md`` and ``.txt`` reviews MUST keep the ``fs_read`` pattern from
 *   the F1 handoff design: point the agent at ``doc_path`` and let it
 *   selectively load passages on demand. Inlining a 50 KB markdown
 *   file every conversation opening would waste ~15K tokens for no
 *   gain -- the agent can read text files itself.
 */
import { describe, it, expect } from 'vitest'
import { buildReviewChatMessage } from '../lib/reviewChatHandoff.prompt'
import type { ReviewContextBundle, ReviewDetail } from '../lib/types'

function makeReviewBundle(
  overrides: {
    docPath?: string
    documentContent?: string
    ask?: string
  } = {},
): ReviewContextBundle {
  const review: ReviewDetail = {
    id: 'test-review-id',
    doc_name: 'sample.md',
    doc_path: overrides.docPath ?? '/tmp/sample.md',
    verdict: 'green',
    finding_count: 0,
    scanners_run: ['clarity'],
    created_at: 1_700_000_000,
    context: {
      audience: 'team',
      doc_type: 'update',
      tone: 'neutral',
      additional_context: [],
      ask: overrides.ask ?? '',
    },
    findings: [],
    partial_failure: false,
    failed_scanners: [],
    log_reference: null,
  }
  return {
    review,
    document_content: overrides.documentContent ?? '',
    scanner_brief_dir: '/tmp/briefs',
  }
}

describe('buildReviewChatMessage', () => {
  it('inlines extracted document_content when the doc_path is a .docx', () => {
    // Docx files cannot be read by the agent via ``fs_read`` because
    // the ZIP bytes fail UTF-8 decoding. The frontend already fetches
    // the extracted prose via ``/reviews/{id}/context`` -- the message
    // builder MUST push that content into the chat handoff.
    const bundle = makeReviewBundle({
      docPath: '/tmp/uploads/abc123_design.docx',
      documentContent:
        '# Network design\n\n' +
        '[VISUAL: An image is embedded...]\n\n' +
        'We will have 2 network racks and a firewall pair.',
    })
    const message = buildReviewChatMessage(bundle)

    expect(message).toContain('Network design')
    expect(message).toContain('We will have 2 network racks and a firewall pair.')
    expect(message).toContain('[VISUAL: An image is embedded')
    // MUST NOT tell the agent to fs_read a docx -- that returns ZIP
    // bytes and the agent silently fails to understand them.
    expect(message).not.toContain('fs_read on /tmp/uploads/abc123_design.docx')
  })

  it('keeps the fs_read pointer for a .md doc_path (F1 pattern)', () => {
    // Markdown / plain-text files are agent-readable via ``fs_read``.
    // The F1 handoff design deliberately avoids inlining doc content
    // for readable formats -- that would waste tokens on every chat
    // opening. The agent selectively reads passages on demand.
    const bundle = makeReviewBundle({
      docPath: '/tmp/sample.md',
      documentContent: 'This content should NOT be inlined for md.',
    })
    const message = buildReviewChatMessage(bundle)

    expect(message).toContain('fs_read on /tmp/sample.md')
    expect(message).not.toContain('This content should NOT be inlined for md.')
  })

  it('keeps the fs_read pointer for a .txt doc_path (F1 pattern)', () => {
    // Same reasoning as .md -- .txt is text-decodable, so ``fs_read``
    // works and the F1 pattern applies. Only binary formats need the
    // inline path.
    const bundle = makeReviewBundle({
      docPath: '/tmp/sample.txt',
      documentContent: 'This content should NOT be inlined for txt.',
    })
    const message = buildReviewChatMessage(bundle)

    expect(message).toContain('fs_read on /tmp/sample.txt')
    expect(message).not.toContain('This content should NOT be inlined for txt.')
  })

  it('inlines document_content when the .docx extension is uppercase', () => {
    // Filename casing on user uploads is unreliable -- accept any case
    // variation. A ``.DOCX`` from a Windows machine must trigger the
    // inline path just like ``.docx``.
    const bundle = makeReviewBundle({
      docPath: '/tmp/uploads/abc_report.DOCX',
      documentContent: '# Report body\n\nQuarterly numbers here.',
    })
    const message = buildReviewChatMessage(bundle)

    expect(message).toContain('Quarterly numbers here.')
    expect(message).not.toContain('fs_read on /tmp/uploads/abc_report.DOCX')
  })
})
