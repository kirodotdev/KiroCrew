import { describe, it, expect } from 'vitest'
import { approvalLabel, changeLabel, skillLabel, toolAccessLabel } from './workerKitLabels'

describe('worker kit preview names', () => {
  it('names a skill by the folder holding its SKILL.md', () => {
    expect(skillLabel('skill:///home/user/.kiro/skills/release-notes/SKILL.md')).toBe('release-notes')
    expect(skillLabel('file:///home/user/notes/checklist.md')).toBe('checklist')
    expect(skillLabel('release-notes')).toBe('release-notes')
  })

  it('names a pattern by the folder it covers', () => {
    expect(skillLabel('skill://~/.kiro/skills/**/SKILL.md')).toBe('Skills in skills')
  })

  it('names an approval by its tool and server', () => {
    expect(approvalLabel('@notes/save_note')).toBe('save_note on notes')
    expect(approvalLabel('@notes')).toBe('Every notes tool')
    expect(approvalLabel('@notes/*')).toBe('Every notes tool')
    expect(approvalLabel('fs_read')).toBe('fs_read')
  })

  it('words a tool selector as what the worker can use, never like an approval', () => {
    expect(toolAccessLabel('@jira')).toBe('Can use every jira tool')
    expect(toolAccessLabel('@jira/*')).toBe('Can use every jira tool')
    expect(toolAccessLabel('@notes/read')).toBe('Can use read on notes')
    expect(toolAccessLabel('fs_read')).toBe('fs_read')
    expect(toolAccessLabel('@jira')).not.toBe(approvalLabel('@jira'))
  })

  it('names a server by its key and the instructions row by nothing', () => {
    expect(changeLabel({ section: 'mcpServers', id: 'weather' })).toBe('weather')
    expect(changeLabel({ section: 'prompt', id: 'prompt' })).toBeNull()
    expect(changeLabel({ section: 'skills', id: 'skill:///a/b/SKILL.md' })).toBe('b')
    expect(changeLabel({ section: 'excludedTools', id: '@jira/delete_issue' })).toBe('delete_issue on jira')
    expect(changeLabel({ section: 'tools', id: '@notes/read' })).toBe('Can use read on notes')
  })
})
