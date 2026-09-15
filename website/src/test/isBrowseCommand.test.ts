import { describe, it, expect } from 'vitest'

// From `utils/browseCommand`, not `../pages/ChatPage`: importing the page pulled
// ~700 eager modules into this fork for a set of string assertions.
import { isBrowseCommand } from '../utils/browseCommand'

/**
 * The Browser panel auto-opens on the agent's own shell command, so this
 * predicate decides when the panel steals focus. A regex that accepted any
 * MENTION of the binary (the first version required only leading whitespace)
 * opened the panel on `grep playwright-cli`, which is a search, not a browse.
 */
describe('isBrowseCommand', () => {
  it('matches a browse at the start of a command', () => {
    expect(isBrowseCommand('playwright-cli open https://example.com')).toBe(true)
    expect(isBrowseCommand('playwright-cli snapshot')).toBe(true)
    expect(isBrowseCommand('playwright-cli')).toBe(true)
  })

  it('matches a browse after a shell separator', () => {
    expect(isBrowseCommand('cd /tmp && playwright-cli open https://x')).toBe(true)
    expect(isBrowseCommand('echo hi; playwright-cli snapshot')).toBe(true)
    expect(isBrowseCommand('(playwright-cli close)')).toBe(true)
    expect(isBrowseCommand('true | playwright-cli list')).toBe(true)
  })

  it('does NOT match a mere mention', () => {
    expect(isBrowseCommand('grep -r playwright-cli .')).toBe(false)
    expect(isBrowseCommand('echo playwright-cli')).toBe(false)
    expect(isBrowseCommand('which playwright-cli')).toBe(false)
    expect(isBrowseCommand('command -v playwright-cli')).toBe(false)
  })

  it('does NOT match a different binary that merely starts the same', () => {
    expect(isBrowseCommand('playwright-cli-wrapper open x')).toBe(false)
    expect(isBrowseCommand('playwright open x')).toBe(false)
  })

  it('parses the JSON tool input, which is what a real preview is', () => {
    // Found on the PR: a real preview is the tool INPUT, so the raw string never
    // matched and the panel never opened.
    expect(isBrowseCommand('{"command":"playwright-cli open https://example.com"}')).toBe(true)
    expect(isBrowseCommand('{"command":"cd /tmp && playwright-cli snapshot"}')).toBe(true)
    expect(isBrowseCommand('{"command":"grep -r playwright-cli ."}')).toBe(false)
    expect(isBrowseCommand('{"summary":"mentions playwright-cli"}')).toBe(false)
    expect(isBrowseCommand('{not json')).toBe(false)
  })

  it('treats an absent preview as not a browse', () => {
    expect(isBrowseCommand(undefined)).toBe(false)
    expect(isBrowseCommand(null)).toBe(false)
    expect(isBrowseCommand('')).toBe(false)
  })
})
