/**
 * The MCP Servers page's Try a tool panel: hidden while `mcp.tool_tester` is off,
 * a form from the input schema, a confirm before a tool not declared read-only, and
 * answers that stay with the server they were asked of.
 * `i18nT` is mocked to the key (plus its values when it has any) so assertions pin keys.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

const { config, tryTool } = vi.hoisted(() => ({ config: vi.fn(), tryTool: vi.fn() }))

vi.mock('../../api/client', () => ({
  api: { kirocrewConfig: (...a: unknown[]) => config(...a), mcpTryTool: (...a: unknown[]) => tryTool(...a) },
}))

vi.mock('../../i18n/t', async importOriginal => {
  const orig = await importOriginal<typeof import('../../i18n/t')>()
  return { ...orig, i18nT: (key: string, values?: Record<string, unknown>) => (values ? `${key}:${JSON.stringify(values)}` : key) }
})

import McpTryTool, { argumentsFrom, hasUnsafeNumber, schemaFields, toolErrorText } from './McpTryTool'

const K = 'pages.overview.mcpTryTool.'
const WRITE_TOOL = {
  name: 'write',
  description: 'Writes a note',
  inputSchema: { type: 'object', properties: { text: { type: 'string' }, count: { type: 'integer' } }, required: ['text'] },
  needsConfirm: true,
  switchedOff: false,
}
const READ_TOOL = { name: 'read', description: 'Reads a note', inputSchema: { type: 'object' }, needsConfirm: false, switchedOff: false }

beforeEach(() => { config.mockReset(); tryTool.mockReset(); config.mockResolvedValue({ mcp: { tool_tester: true } }) })

describe('schemaFields and argumentsFrom', () => {
  it('builds fields for the simple types and falls back for anything else', () => {
    expect(schemaFields({ type: 'object', properties: { a: { type: 'string' }, b: { enum: ['x', 'y'] } }, required: ['a'] })).toEqual([
      { name: 'a', kind: 'string', options: [], required: true },
      { name: 'b', kind: 'enum', options: ['x', 'y'], required: false },
    ])
    expect(schemaFields({ type: 'object', properties: { a: { type: 'array' } } })).toBeNull()
    expect(schemaFields({})).toEqual([])
  })

  it('gives a composed or referenced schema the raw JSON box, so a required argument stays reachable', () => {
    expect(schemaFields({ type: 'object', allOf: [{ properties: { query: { type: 'string' } }, required: ['query'] }] })).toBeNull()
    for (const key of ['anyOf', 'oneOf', 'not', '$ref']) expect(schemaFields({ type: 'object', [key]: key === '$ref' ? '#/$defs/args' : [] })).toBeNull()
  })

  it("reads the server's own text out of a tool error, or nothing", () => {
    expect(toolErrorText('{"content": [{"type": "text", "text": " Quota service is not reachable "}], "isError": true}')).toBe('Quota service is not reachable')
    expect(toolErrorText('{"code": -32000, "message": "no such note"}')).toBe('no such note')
    expect(toolErrorText('{"isError": true}')).toBe('')
    expect(toolErrorText('{"cut off')).toBe('')
  })

  it('types the values, leaves out an unset optional one and names a bad one', () => {
    const fields = schemaFields(WRITE_TOOL.inputSchema)!
    expect(argumentsFrom(fields, { text: 'hi', count: ' 3 ' })).toEqual({ ok: true, args: { text: 'hi', count: 3 } })
    expect(argumentsFrom(fields, { text: 'hi' })).toEqual({ ok: true, args: { text: 'hi' } })
    expect(argumentsFrom(fields, { text: 'hi', count: '1.5' })).toEqual({ ok: false, invalid: 'count' })
    expect(argumentsFrom(fields, {})).toEqual({ ok: false, invalid: 'text' })
  })

  it('names an integer outside the safe range instead of sending a different number', () => {
    const fields = schemaFields(WRITE_TOOL.inputSchema)!
    expect(argumentsFrom(fields, { text: 'hi', count: '9007199254740991' })).toEqual({ ok: true, args: { text: 'hi', count: 9007199254740991 } })
    expect(argumentsFrom(fields, { text: 'hi', count: '-9007199254740991' })).toEqual({ ok: true, args: { text: 'hi', count: -9007199254740991 } })
    for (const count of ['9007199254740992', '9007199254740993', '-9007199254740993', '1e30', '1' + '0'.repeat(400)]) {
      expect(argumentsFrom(fields, { text: 'hi', count })).toEqual({ ok: false, invalid: 'count' })
    }
    // A number field takes any finite number, as before.
    const loose = schemaFields({ type: 'object', properties: { x: { type: 'number' } } })!
    expect(argumentsFrom(loose, { x: '9007199254740993' })).toEqual({ ok: true, args: { x: 9007199254740992 } })
  })

  it('finds an integer literal past the safe range outside strings only', () => {
    expect(hasUnsafeNumber('{"a": 9007199254740993}')).toBe(true)
    expect(hasUnsafeNumber('{"a": [1, -9007199254740993]}')).toBe(true)
    expect(hasUnsafeNumber('{"a": 1' + '0'.repeat(400) + '}')).toBe(true)
    expect(hasUnsafeNumber('{"a": 9007199254740991, "b": -9007199254740991, "c": 0, "d": -0}')).toBe(false)
    expect(hasUnsafeNumber('{"a": 9007199254740993.5, "b": 1e30, "c": 12345678901234567E-3}')).toBe(false)
    expect(hasUnsafeNumber('{"id": "9007199254740993", "q": "say \\"9007199254740993\\" 9007199254740993"}')).toBe(false)
    expect(hasUnsafeNumber('{"id": "a\\\\", "n": 9007199254740993}')).toBe(true)
    expect(hasUnsafeNumber('')).toBe(false)
  })

  it('finds a fraction or an exponent too large to be finite outside strings only', () => {
    expect(hasUnsafeNumber('{"value": 1e400}')).toBe(true)
    expect(hasUnsafeNumber('{"value": -1e400}')).toBe(true)
    expect(hasUnsafeNumber('[{"a": [1e400]}]')).toBe(true)
    expect(hasUnsafeNumber('{"value": 1' + '0'.repeat(400) + '.5}')).toBe(true)
    expect(hasUnsafeNumber('{"value": "1e400"}')).toBe(false)
    expect(hasUnsafeNumber('{"value": 1e21}')).toBe(false)
    expect(hasUnsafeNumber('{"value": 1.5}')).toBe(false)
  })

  it('sends strings and enums as typed and tells an unset field from an empty string', () => {
    const fields = schemaFields({ type: 'object', properties: { a: { type: 'string' }, b: { type: 'string' }, c: { enum: ['x'] } }, required: ['a'] })!
    expect(argumentsFrom(fields, { a: '  indented\n  text  ', b: '' })).toEqual({ ok: true, args: { a: '  indented\n  text  ', b: '' } })
    expect(argumentsFrom(fields, { a: '   ' })).toEqual({ ok: true, args: { a: '   ' } })
    expect(argumentsFrom(fields, { a: '', c: '' })).toEqual({ ok: true, args: { a: '' } })
    expect(argumentsFrom(fields, { b: 'x' })).toEqual({ ok: false, invalid: 'a' })
  })

  it('keeps an argument named invalid apart from the validation result', () => {
    const fields = schemaFields({ type: 'object', properties: { invalid: { type: 'string' } }, required: ['invalid'] })!
    expect(argumentsFrom(fields, { invalid: 'yes' })).toEqual({ ok: true, args: { invalid: 'yes' } })
  })
})

describe('McpTryTool', () => {
  it('draws nothing while the setting is off', async () => {
    config.mockResolvedValue({ mcp: { tool_tester: false } })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    await waitFor(() => expect(config).toHaveBeenCalled())
    expect(screen.queryByTestId('mcp-try-tool')).toBeNull()
  })

  it('says so when the setting cannot be read, loudly and with the agent hand-off', async () => {
    config.mockRejectedValue(new Error('gateway unreachable'))
    renderWithProviders(<McpTryTool servers={['notes']} />)
    const notice = await screen.findByTestId('mcp-try-tool-config-error')
    expect(notice.textContent).toContain('gateway unreachable')
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain('components.askAgent.ask_the_agent')
    expect(screen.queryByTestId('mcp-try-tool')).toBeNull()
  })

  it('keeps the panel and what is typed in it when a refresh of the setting fails', async () => {
    tryTool.mockResolvedValueOnce({ ok: true, tools: [WRITE_TOOL] })
    const { queryClient } = renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByText('Writes a note')
    fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'draft' } })
    config.mockRejectedValue(new Error('gateway unreachable'))
    await queryClient.refetchQueries({ queryKey: ['kirocrewConfig'] })
    const notice = await screen.findByTestId('mcp-try-tool-config-refresh-error')
    expect(notice.textContent).toContain('gateway unreachable')
    expect(screen.getByTestId('mcp-try-tool')).toBeTruthy()
    expect((screen.getAllByRole('textbox')[0] as HTMLInputElement).value).toBe('draft')
  })

  it('uses the first server once the list arrives after the panel is drawn', async () => {
    tryTool.mockResolvedValue({ ok: true, tools: [READ_TOOL] })
    const { rerender } = renderWithProviders(<McpTryTool servers={[]} />)
    const load = await screen.findByText(`${K}load_tools`)
    expect((load.closest('button') as HTMLButtonElement).disabled).toBe(true)
    rerender(<McpTryTool servers={['notes', 'calendar']} />)
    await waitFor(() => expect((screen.getByText(`${K}load_tools`).closest('button') as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByText(`${K}load_tools`))
    await waitFor(() => expect(tryTool).toHaveBeenCalledWith({ server: 'notes' }))
  })

  it('drops a listing that lands after the user chose another server', async () => {
    let answerNotes: (v: unknown) => void = () => {}
    tryTool.mockImplementation(() => new Promise(resolve => { answerNotes = resolve }))
    renderWithProviders(<McpTryTool servers={['notes', 'calendar']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await waitFor(() => expect(tryTool).toHaveBeenCalledWith({ server: 'notes' }))
    fireEvent.change(screen.getByLabelText(`${K}server`), { target: { value: 'calendar' } })
    answerNotes({ ok: true, tools: [WRITE_TOOL] })
    await waitFor(() => expect((screen.getByText(`${K}load_tools`).closest('button') as HTMLButtonElement).disabled).toBe(false))
    expect(screen.queryByText('Writes a note')).toBeNull()
    expect(screen.queryByLabelText(`${K}tool`)).toBeNull()
  })

  it('asks plainly before running a tool not declared read-only, then sends the confirm', async () => {
    tryTool.mockResolvedValueOnce({ ok: true, tools: [WRITE_TOOL] })
    tryTool.mockResolvedValueOnce({ ok: true, result: '{"content": []}', isError: false, truncated: true })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    expect(await screen.findByText('Writes a note')).toBeTruthy()
    expect(tryTool).toHaveBeenLastCalledWith({ server: 'notes' })

    // The risk is named before the press: the label promises a further step and a line says why.
    expect(screen.queryByText(`${K}run`)).toBeNull()
    const runButton = screen.getByText(`${K}run_needs_confirm`).closest('button')!
    const line = screen.getByTestId('mcp-try-tool-changes-data')
    expect(line.textContent).toContain(`${K}changes_data`)
    expect(runButton.getAttribute('aria-describedby')).toBe(line.id)
    fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: ' hello ' } })
    fireEvent.click(runButton)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain(`${K}confirm_text:{"tool":"write","server":"notes"}`)
    expect(tryTool).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByText(`${K}confirm_run:{"tool":"write"}`))
    await waitFor(() => expect(tryTool).toHaveBeenCalledTimes(2))
    expect(tryTool).toHaveBeenLastCalledWith({ server: 'notes', tool: 'write', arguments: { text: ' hello ' }, confirm: true })
    expect((await screen.findByTestId('mcp-try-tool-result')).textContent).toContain('{"content": []}')
    expect(screen.getByText(`${K}truncated`)).toBeTruthy()
  })

  it('shows a bad argument as a hint, apart from request errors', async () => {
    tryTool.mockResolvedValueOnce({ ok: true, tools: [WRITE_TOOL] })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByText('Writes a note')
    fireEvent.click(screen.getByText(`${K}run_needs_confirm`))
    expect((await screen.findByTestId('mcp-try-tool-hint')).textContent).toBe(`${K}invalid_field:{"name":"text"}`)
    expect(screen.queryByTestId('mcp-try-tool-error')).toBeNull()
    expect(tryTool).toHaveBeenCalledTimes(1)
  })

  it('keeps the answer of a run from landing under another tool', async () => {
    let answerRun: (v: unknown) => void = () => {}
    tryTool.mockResolvedValueOnce({ ok: true, tools: [READ_TOOL, { ...READ_TOOL, name: 'other', description: 'Another tool' }] })
    tryTool.mockImplementationOnce(() => new Promise(resolve => { answerRun = resolve }))
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByText('Reads a note')
    // A read-only tool keeps the plain label and no warning line.
    expect(screen.queryByTestId('mcp-try-tool-changes-data')).toBeNull()
    fireEvent.click(screen.getByText(`${K}run`))
    await waitFor(() => expect(tryTool).toHaveBeenCalledTimes(2))
    // A run in flight looks busy, not switched off, and a second press is swallowed.
    const running = screen.getByText(`${K}run`).closest('button')!
    expect(running.disabled).toBe(false)
    expect(running.getAttribute('aria-busy')).toBe('true')
    fireEvent.click(running)
    expect(tryTool).toHaveBeenCalledTimes(2)
    const picker = screen.getByLabelText(`${K}tool`)
    expect(picker.getAttribute('aria-disabled')).toBe('true')
    // A change that reaches the picker during the run is refused, and a stale answer is dropped.
    fireEvent.change(picker, { target: { value: 'other' } })
    expect(screen.getByText('Reads a note')).toBeTruthy()
    answerRun({ ok: true, result: '{"from": "read"}', isError: false })
    await waitFor(() => expect(screen.getByLabelText(`${K}tool`).getAttribute('aria-disabled')).toBe('false'))
    expect((await screen.findByTestId('mcp-try-tool-result')).textContent).toContain('"from": "read"')
    fireEvent.change(screen.getByLabelText(`${K}tool`), { target: { value: 'other' } })
    expect(await screen.findByText('Another tool')).toBeTruthy()
    expect(screen.queryByTestId('mcp-try-tool-result')).toBeNull()
  })

  it('keeps the line breaks typed or pasted into a string argument', async () => {
    const tool = { ...READ_TOOL, inputSchema: { type: 'object', properties: { text: { type: 'string' }, count: { type: 'integer' } } } }
    tryTool.mockResolvedValueOnce({ ok: true, tools: [tool] })
    tryTool.mockResolvedValueOnce({ ok: true, result: '{}', isError: false })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByText('Reads a note')
    const [text, count] = screen.getAllByRole('textbox')
    // A string gets a box that can hold several lines; a number stays a one-line input.
    expect(text.tagName).toBe('TEXTAREA')
    expect(count.tagName).toBe('INPUT')
    fireEvent.change(text, { target: { value: 'line one\nline two' } })
    // The box grows with the text, so no line is hidden behind a scroll.
    expect(text.getAttribute('rows')).toBe('2')
    fireEvent.click(screen.getByText(`${K}run`))
    await waitFor(() => expect(tryTool).toHaveBeenLastCalledWith({ server: 'notes', tool: 'read', arguments: { text: 'line one\nline two' }, confirm: false }))
  })

  it('runs a tool whose argument is named invalid', async () => {
    const tool = { ...READ_TOOL, inputSchema: { type: 'object', properties: { invalid: { type: 'string' } }, required: ['invalid'] } }
    tryTool.mockResolvedValueOnce({ ok: true, tools: [tool] })
    tryTool.mockResolvedValueOnce({ ok: true, result: '{}', isError: false })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByText('Reads a note')
    fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'yes' } })
    fireEvent.click(screen.getByText(`${K}run`))
    await waitFor(() => expect(tryTool).toHaveBeenLastCalledWith({ server: 'notes', tool: 'read', arguments: { invalid: 'yes' }, confirm: false }))
    expect(screen.queryByTestId('mcp-try-tool-hint')).toBeNull()
  })

  it('shows a tool error through the error notice and keeps the raw result', async () => {
    tryTool.mockResolvedValueOnce({ ok: true, tools: [READ_TOOL] })
    tryTool.mockResolvedValueOnce({ ok: true, result: '{"isError": true, "detail": "no such note"}', isError: true })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByText('Reads a note')
    fireEvent.click(screen.getByText(`${K}run`))
    const result = await screen.findByTestId('mcp-try-tool-result')
    const notice = await screen.findByTestId('mcp-try-tool-tool-error')
    expect(notice.textContent).toContain(`${K}tool_error`)
    expect(notice.getAttribute('role')).toBe('alert')
    expect(result.textContent).toContain('no such note')
  })

  it("leads a tool error with the server's own text", async () => {
    tryTool.mockResolvedValueOnce({ ok: true, tools: [READ_TOOL] })
    tryTool.mockResolvedValueOnce({ ok: true, result: '{"content": [{"type": "text", "text": "Quota service is not reachable"}], "isError": true}', isError: true })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByText('Reads a note')
    fireEvent.click(screen.getByText(`${K}run`))
    const notice = await screen.findByTestId('mcp-try-tool-tool-error')
    expect(notice.textContent).toContain(`${K}tool_error`)
    expect(notice.textContent).toContain('Quota service is not reachable')
  })

  it('names how many tools the server left out of the list', async () => {
    tryTool.mockResolvedValueOnce({ ok: true, tools: [READ_TOOL], leftOut: 7 })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    expect((await screen.findByTestId('mcp-try-tool-left-out')).textContent).toBe(`${K}left_out:{"count":7}`)
  })
})

describe('McpTryTool discarded schemas, withheld names and failed runs', () => {
  it('gives a discarded (null) schema the raw box while {} stays a tool with no arguments', () => {
    expect(schemaFields(null)).toBeNull()
    expect(schemaFields({})).toEqual([])
  })

  it('sends the raw JSON of a tool whose schema was discarded, so a required argument is reachable', async () => {
    tryTool.mockResolvedValueOnce({ ok: true, tools: [{ ...READ_TOOL, inputSchema: null }] })
    tryTool.mockResolvedValueOnce({ ok: true, result: '{}', isError: false })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByText('Reads a note')
    const box = screen.getByLabelText(`${K}raw_arguments`)
    fireEvent.change(box, { target: { value: '{"query": "x"}' } })
    fireEvent.click(screen.getByText(`${K}run`))
    await waitFor(() => expect(tryTool).toHaveBeenLastCalledWith({ server: 'notes', tool: 'read', arguments: { query: 'x' }, confirm: false }))
  })

  it('refuses a raw JSON integer outside the safe range with the invalid JSON hint', async () => {
    tryTool.mockResolvedValueOnce({ ok: true, tools: [{ ...READ_TOOL, inputSchema: null }] })
    tryTool.mockResolvedValueOnce({ ok: true, result: '{}', isError: false })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByText('Reads a note')
    const box = screen.getByLabelText(`${K}raw_arguments`)
    fireEvent.change(box, { target: { value: '{"id": 9007199254740993}' } })
    fireEvent.click(screen.getByText(`${K}run`))
    expect((await screen.findByTestId('mcp-try-tool-hint')).textContent).toBe(`${K}invalid_json`)
    expect(tryTool).toHaveBeenCalledTimes(1)
    // The same number as text is not an integer literal, so it goes out as typed.
    fireEvent.change(box, { target: { value: '{"id": "9007199254740993"}' } })
    fireEvent.click(screen.getByText(`${K}run`))
    await waitFor(() => expect(tryTool).toHaveBeenLastCalledWith({ server: 'notes', tool: 'read', arguments: { id: '9007199254740993' }, confirm: false }))
  })

  it('refuses a raw JSON number too large to be finite with the invalid JSON hint', async () => {
    tryTool.mockResolvedValueOnce({ ok: true, tools: [{ ...READ_TOOL, inputSchema: null }] })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByText('Reads a note')
    fireEvent.change(screen.getByLabelText(`${K}raw_arguments`), { target: { value: '{"value":1e400}' } })
    fireEvent.click(screen.getByText(`${K}run`))
    expect((await screen.findByTestId('mcp-try-tool-hint')).textContent).toBe(`${K}invalid_json`)
    // Only the listing went out; the run that would have sent null never did.
    expect(tryTool).toHaveBeenCalledTimes(1)
  })

  it('names the tools withheld for their names only when there are some', async () => {
    tryTool.mockResolvedValueOnce({ ok: true, tools: [READ_TOOL], leftOut: 0, withheld: 3 })
    const first = renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    expect((await screen.findByTestId('mcp-try-tool-withheld')).textContent).toBe(`${K}withheld:{"count":3}`)
    first.unmount()
    tryTool.mockResolvedValueOnce({ ok: true, tools: [READ_TOOL], leftOut: 0, withheld: 0 })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByText('Reads a note')
    expect(screen.queryByTestId('mcp-try-tool-withheld')).toBeNull()
  })

  it("titles a failed run above the server's text, and leaves a failed listing untitled", async () => {
    tryTool.mockRejectedValueOnce(new Error('spawn failed'))
    const first = renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    const listing = await screen.findByTestId('mcp-try-tool-error')
    expect(listing.textContent).toContain('spawn failed')
    expect(listing.textContent).not.toContain(`${K}run_failed`)
    first.unmount()

    tryTool.mockResolvedValueOnce({ ok: true, tools: [READ_TOOL] })
    tryTool.mockRejectedValueOnce(new Error('no answer in time'))
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByText('Reads a note')
    fireEvent.click(screen.getByText(`${K}run`))
    const notice = await screen.findByTestId('mcp-try-tool-error')
    expect(notice.textContent).toContain(`${K}run_failed`)
    expect(notice.textContent).toContain('no answer in time')
  })
})

describe('McpTryTool first tool and stale feedback', () => {
  const OFF_TOOL = { ...READ_TOOL, name: 'off', description: 'Switched off', switchedOff: true }
  const OTHER_TOOL = { ...READ_TOOL, name: 'other', description: 'Another tool' }

  it('selects the first tool that is not switched off, and runs it', async () => {
    tryTool.mockResolvedValueOnce({ ok: true, tools: [OFF_TOOL, READ_TOOL] })
    tryTool.mockResolvedValueOnce({ ok: true, result: '{}', isError: false })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByText('Reads a note')
    expect((screen.getByLabelText(`${K}tool`) as HTMLSelectElement).value).toBe('read')
    fireEvent.click(screen.getByText(`${K}run`))
    await waitFor(() => expect(tryTool).toHaveBeenLastCalledWith({ server: 'notes', tool: 'read', arguments: {}, confirm: false }))
  })

  it('selects nothing and keeps Run disabled when every tool is switched off', async () => {
    tryTool.mockResolvedValueOnce({ ok: true, tools: [OFF_TOOL, { ...OFF_TOOL, name: 'off2' }] })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByLabelText(`${K}tool`)
    expect((screen.getByLabelText(`${K}tool`) as HTMLSelectElement).value).toBe('')
    expect(screen.queryByText('Switched off')).toBeNull()
    expect((screen.getByText(`${K}run`).closest('button') as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByText(`${K}run`))
    expect(tryTool).toHaveBeenCalledTimes(1)
  })

  it('clears a failed run, its hint and its confirm when the tool changes', async () => {
    tryTool.mockResolvedValueOnce({ ok: true, tools: [WRITE_TOOL, OTHER_TOOL] })
    tryTool.mockRejectedValueOnce(new Error('no answer in time'))
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByText('Writes a note')
    // A missing required argument is a hint; once it is filled in, the confirm and then a failed run follow.
    fireEvent.click(screen.getByText(`${K}run_needs_confirm`))
    await screen.findByTestId('mcp-try-tool-hint')
    fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'hi' } })
    fireEvent.click(screen.getByText(`${K}run_needs_confirm`))
    fireEvent.click(await screen.findByText(`${K}confirm_run:{"tool":"write"}`))
    expect((await screen.findByTestId('mcp-try-tool-error')).textContent).toContain('no answer in time')
    fireEvent.change(screen.getByLabelText(`${K}tool`), { target: { value: 'other' } })
    await screen.findByText('Another tool')
    expect(screen.queryByTestId('mcp-try-tool-error')).toBeNull()
    expect(screen.queryByTestId('mcp-try-tool-hint')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('clears an answer when the server changes, also through a refresh of the server list', async () => {
    tryTool.mockResolvedValueOnce({ ok: true, tools: [READ_TOOL] })
    tryTool.mockResolvedValueOnce({ ok: true, result: '{"from": "notes"}', isError: false })
    const { rerender } = renderWithProviders(<McpTryTool servers={['notes', 'calendar']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByText('Reads a note')
    fireEvent.click(screen.getByText(`${K}run`))
    expect((await screen.findByTestId('mcp-try-tool-result')).textContent).toContain('"from": "notes"')
    // The refresh drops the server that was chosen, so the effective server is now calendar.
    rerender(<McpTryTool servers={['calendar']} />)
    await waitFor(() => expect(screen.queryByTestId('mcp-try-tool-result')).toBeNull())
    expect((screen.getByLabelText(`${K}server`) as HTMLSelectElement).value).toBe('calendar')
  })

  it('clears a failed run when the server changes through a refresh of the server list', async () => {
    tryTool.mockRejectedValueOnce(new Error('spawn failed'))
    const { rerender } = renderWithProviders(<McpTryTool servers={['notes', 'calendar']} />)
    fireEvent.click(await screen.findByText(`${K}load_tools`))
    await screen.findByTestId('mcp-try-tool-error')
    rerender(<McpTryTool servers={['calendar']} />)
    await waitFor(() => expect(screen.queryByTestId('mcp-try-tool-error')).toBeNull())
  })
})
