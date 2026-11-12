import { describe, it, expect } from 'vitest'
import { promptPreview } from '../utils/pinnedPrompt'

// The pinned prompt's one-line preview names each attachment by its file name.
describe('promptPreview attachment names', () => {
  it('shows the file name of a Windows drive path', () => {
    expect(promptPreview('review [attached_file 1] C:\\Users\\me\\Documents\\report.docx please'))
      .toBe('review report.docx please')
  })

  it('shows the file name of a UNC path', () => {
    expect(promptPreview('see [attached_file 1] \\\\host\\share\\notes.md'))
      .toBe('see notes.md')
  })

  it('keeps a POSIX file name that contains a backslash whole', () => {
    expect(promptPreview('see [attached_file 1] /tmp/we\\ird.md'))
      .toBe('see we\\ird.md')
  })

  it('shows the file name of a POSIX path', () => {
    expect(promptPreview('see [attached_file 1] /repo/src/a.ts now'))
      .toBe('see a.ts now')
  })
})
