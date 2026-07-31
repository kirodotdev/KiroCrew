/**
 * LibraryPanel — the style and template library.
 *
 * Two lists, one detail pane. A style is the visual mood; a template is the .pptx
 * slide layouts. Bundled entries are read-only; the user's own can be renamed,
 * deleted, pinned (styles) and imported.
 *
 * Every mutation goes through React Query so the list refreshes from the server
 * rather than from optimistic local state — the agent writes styles too, so the
 * server is the only honest source of what exists.
 */

import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Palette, Pencil, Pin, Plus, Trash2, Type } from 'lucide-react'
import { Btn, Card, CardTitle, EmptyState, Input, SendBtn } from '../../components/ui'
import Clickable from '../../components/Clickable'
import InfoTip from '../../components/InfoTip'
import { i18nT } from '../../i18n/t'
import { pptxMakerApi, type StyleEntry, type TemplateEntry } from './api'
import { BoardThumb } from './BoardFrame'
import BoardFrame from './BoardFrame'
import { nameFromFilename, templateAccents } from './lib'

/**
 * The chat-token labels, deliberately NOT catalog values.
 *
 * The panel shows the literal token the user pastes into chat (`[Style: name]`),
 * and the agent prompts parse exactly that spelling — see
 * `builtin_skills/pptx-maker/SKILL.md` and `prompts/spec-studio.md`. A translated
 * label would produce a token the agent does not recognise, so per
 * `website/AGENTS.md` ("a literal token the user must type must never be a
 * catalog value") these stay code constants.
 */
const STYLE_TOKEN_LABEL = 'Style'
const TEMPLATE_TOKEN_LABEL = 'Template'

/**
 * Font specimen for the template's theme preview. Not translated: it exists to
 * SHOW the typeface's letterforms at a glance, so the Latin pair is the content
 * rather than a label — the same reason the type ramp in a font picker is not
 * localized.
 */
const FONT_SPECIMEN = 'Aa'

type LibraryKind = 'styles' | 'templates'

function isUserOwned(entry: { source?: string }): boolean {
  return entry.source === 'user'
}

/** Inline rename control — shown in place of the detail header while renaming. */
function RenameRow({
  initial,
  error,
  onSubmit,
  onCancel,
}: {
  initial: string
  error: string
  onSubmit: (value: string) => void
  onCancel: () => void
}) {
  const [value, setValue] = useState(initial)
  return (
    <div className="flex items-center gap-2 mb-3 flex-wrap">
      <Input
        autoFocus
        value={value}
        aria-label={i18nT('apps.pptxMaker.libraryPanel.new_name')}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') onSubmit(value.trim())
          if (event.key === 'Escape') onCancel()
        }}
        className="flex-1 min-w-[160px]"
      />
      <SendBtn onClick={() => onSubmit(value.trim())}>
        {i18nT('apps.pptxMaker.libraryPanel.save')}
      </SendBtn>
      <Btn onClick={onCancel}>{i18nT('apps.pptxMaker.libraryPanel.cancel')}</Btn>
      {error && <span className="text-[12px] text-danger w-full">{error}</span>}
    </div>
  )
}

export default function LibraryPanel({ kind }: { kind: LibraryKind }) {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<string | null>(null)
  const [renaming, setRenaming] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  const isStyles = kind === 'styles'
  const listKey = ['pptx-maker', kind]

  const stylesQuery = useQuery({
    queryKey: ['pptx-maker', 'styles'],
    queryFn: () => pptxMakerApi.styles(),
    enabled: isStyles,
  })
  const templatesQuery = useQuery({
    queryKey: ['pptx-maker', 'templates'],
    queryFn: () => pptxMakerApi.templates(),
    enabled: !isStyles,
  })

  const styles: StyleEntry[] = stylesQuery.data?.styles ?? []
  const templates: TemplateEntry[] = templatesQuery.data?.templates ?? []
  const entries: Array<StyleEntry | TemplateEntry> = isStyles ? styles : templates
  const loading = isStyles ? stylesQuery.isLoading : templatesQuery.isLoading

  const styleHtmlQuery = useQuery({
    queryKey: ['pptx-maker', 'style-html', selected ?? ''],
    queryFn: () => pptxMakerApi.style(selected as string),
    enabled: isStyles && Boolean(selected),
    // The style-creator agent rewrites the document as the conversation goes, so
    // an open style is re-read on a slow poll to show those edits live.
    refetchInterval: 4000,
  })

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: listKey })
  }

  const importMutation = useMutation({
    mutationFn: async (file: File) => {
      const name = nameFromFilename(file.name)
      return isStyles
        ? pptxMakerApi.importStyle(name, file)
        : pptxMakerApi.importTemplate(name, file)
    },
    onSuccess: (_result, file) => {
      setError('')
      setNotice('')
      setSelected(nameFromFilename(file.name))
      refresh()
    },
    onError: (err: Error) => setError(err.message),
  })

  const renameMutation = useMutation({
    mutationFn: (vars: { name: string; to: string }) =>
      isStyles
        ? pptxMakerApi.renameStyle(vars.name, vars.to)
        : pptxMakerApi.renameTemplate(vars.name, vars.to),
    onSuccess: (_result, vars) => {
      setError('')
      setRenaming(false)
      setSelected(vars.to)
      refresh()
    },
    onError: (err: Error) => setError(err.message),
  })

  const deleteMutation = useMutation({
    mutationFn: (name: string) =>
      isStyles ? pptxMakerApi.deleteStyle(name) : pptxMakerApi.deleteTemplate(name),
    onSuccess: () => {
      setError('')
      setSelected(null)
      refresh()
    },
    onError: (err: Error) => setError(err.message),
  })

  const pinMutation = useMutation({
    mutationFn: (vars: { name: string; pinned: boolean }) =>
      pptxMakerApi.pinStyle(vars.name, vars.pinned),
    onSuccess: refresh,
    onError: (err: Error) => setError(err.message),
  })

  const current = entries.find((entry) => entry.name === selected)
  const mutable = current ? isUserOwned(current) : false
  const currentStyle = isStyles ? (current as StyleEntry | undefined) : undefined
  const currentTemplate = !isStyles ? (current as TemplateEntry | undefined) : undefined

  const accept = isStyles ? '.html' : '.pptx'

  const renderRow = (entry: StyleEntry | TemplateEntry) => {
    const active = entry.name === selected
    const style = isStyles ? (entry as StyleEntry) : undefined
    const template = !isStyles ? (entry as TemplateEntry) : undefined
    return (
      <Clickable
        key={entry.name}
        onClick={() => {
          setSelected(entry.name)
          setRenaming(false)
          setError('')
        }}
        className={`w-full flex items-center gap-2 text-left px-2 py-1.5 rounded-md mb-1 cursor-pointer transition-colors hover:bg-bg-elevated ${
          active ? 'bg-bg-elevated' : ''
        }`}
      >
        {style ? (
          <BoardThumb html={style.coverHtml ?? ''} />
        ) : (
          <div className="flex gap-0.5 shrink-0">
            {templateAccents(template?.theme_colors)
              .slice(0, 4)
              .map((color) => (
                <span
                  key={color}
                  className="w-2.5 h-2.5 rounded-full"
                  style={{ background: color }}
                />
              ))}
          </div>
        )}
        <div className="min-w-0 flex-1">
          <div className={`truncate text-sm ${active ? 'text-accent' : 'text-text'}`}>
            {entry.name}
          </div>
          {style?.pinned && (
            <div className="text-[12px] text-accent">
              {i18nT('apps.pptxMaker.libraryPanel.pinned')}
            </div>
          )}
        </div>
      </Clickable>
    )
  }

  const section = (title: string, rows: Array<StyleEntry | TemplateEntry>) =>
    rows.length > 0 && (
      <div className="mb-3">
        <div className="text-[12px] uppercase tracking-[.04em] text-muted px-1 mb-1">{title}</div>
        {rows.map(renderRow)}
      </div>
    )

  return (
    <Card className="flex flex-col min-h-0 flex-1">
      <CardTitle>
        {isStyles
          ? i18nT('apps.pptxMaker.libraryPanel.styles')
          : i18nT('apps.pptxMaker.libraryPanel.templates')}{' '}
        <InfoTip
          text={
            isStyles
              ? i18nT('apps.pptxMaker.libraryPanel.styles_tip')
              : i18nT('apps.pptxMaker.libraryPanel.templates_tip')
          }
        />
      </CardTitle>

      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <SendBtn onClick={() => fileRef.current?.click()} disabled={importMutation.isPending}>
          <Plus className="lucide-inline" />
          {isStyles
            ? i18nT('apps.pptxMaker.libraryPanel.import_style')
            : i18nT('apps.pptxMaker.libraryPanel.import_template')}
        </SendBtn>
        <span className="text-[12px] text-muted">
          {isStyles
            ? i18nT('apps.pptxMaker.libraryPanel.accepts_html')
            : i18nT('apps.pptxMaker.libraryPanel.accepts_pptx')}
        </span>
        <input
          ref={fileRef}
          type="file"
          accept={accept}
          className="hidden"
          aria-label={
            isStyles
              ? i18nT('apps.pptxMaker.libraryPanel.import_style')
              : i18nT('apps.pptxMaker.libraryPanel.import_template')
          }
          onChange={(event) => {
            const file = event.target.files?.[0]
            event.target.value = ''
            if (file) importMutation.mutate(file)
          }}
        />
      </div>

      {error && (
        <div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 text-[13px] text-danger animate-rise">
          {error}
        </div>
      )}
      {notice && <div className="mb-3 text-[13px] text-muted">{notice}</div>}

      <div className="flex gap-4 flex-1 min-h-0">
        <div className="w-56 shrink-0 overflow-y-auto border-r border-border pr-3">
          {loading && (
            <div className="text-sm text-muted px-1">
              {i18nT('apps.pptxMaker.libraryPanel.loading')}
            </div>
          )}
          {!loading && entries.length === 0 && (
            <EmptyState
              icon={
                isStyles ? (
                  <Palette className="lucide-inline" />
                ) : (
                  <Type className="lucide-inline" />
                )
              }
              title={
                isStyles
                  ? i18nT('apps.pptxMaker.libraryPanel.no_styles_yet')
                  : i18nT('apps.pptxMaker.libraryPanel.no_templates_yet')
              }
            />
          )}
          {section(
            i18nT('apps.pptxMaker.libraryPanel.mine'),
            entries.filter((entry) => isUserOwned(entry)),
          )}
          {section(
            i18nT('apps.pptxMaker.libraryPanel.built_in'),
            entries.filter((entry) => !isUserOwned(entry)),
          )}
        </div>

        <div className="flex-1 min-w-0 overflow-y-auto">
          {!current && (
            <div className="text-sm text-muted">
              {isStyles
                ? i18nT('apps.pptxMaker.libraryPanel.select_a_style')
                : i18nT('apps.pptxMaker.libraryPanel.select_a_template')}
            </div>
          )}

          {current && renaming && (
            <RenameRow
              initial={current.name}
              error=""
              onSubmit={(value) => {
                if (!value || value === current.name) {
                  setRenaming(false)
                  return
                }
                renameMutation.mutate({ name: current.name, to: value })
              }}
              onCancel={() => setRenaming(false)}
            />
          )}

          {current && !renaming && (
            <div className="flex items-center gap-2 mb-3 flex-wrap">
              <div className="text-sm font-medium truncate flex-1 min-w-0">{current.name}</div>
              {isStyles && (
                <Btn
                  aria-label={
                    currentStyle?.pinned
                      ? i18nT('apps.pptxMaker.libraryPanel.unpin_style')
                      : i18nT('apps.pptxMaker.libraryPanel.pin_style')
                  }
                  title={
                    currentStyle?.pinned
                      ? i18nT('apps.pptxMaker.libraryPanel.unpin_style')
                      : i18nT('apps.pptxMaker.libraryPanel.pin_style')
                  }
                  onClick={() =>
                    pinMutation.mutate({
                      name: current.name,
                      pinned: !currentStyle?.pinned,
                    })
                  }
                  className={currentStyle?.pinned ? 'text-accent' : ''}
                >
                  <Pin className="lucide-inline" />
                </Btn>
              )}
              {mutable && (
                <Btn
                  aria-label={i18nT('apps.pptxMaker.libraryPanel.rename')}
                  title={i18nT('apps.pptxMaker.libraryPanel.rename')}
                  onClick={() => {
                    setError('')
                    setRenaming(true)
                  }}
                >
                  <Pencil className="lucide-inline" />
                </Btn>
              )}
              {mutable && (
                <Btn
                  danger
                  aria-label={i18nT('apps.pptxMaker.libraryPanel.delete')}
                  title={i18nT('apps.pptxMaker.libraryPanel.delete')}
                  onClick={() => deleteMutation.mutate(current.name)}
                >
                  <Trash2 className="lucide-inline" />
                </Btn>
              )}
            </div>
          )}

          {current && (
            <div className="text-[12px] text-muted mb-3">
              {i18nT('apps.pptxMaker.libraryPanel.reference_in_chat')}{' '}
              <code className="font-mono text-text">
                [{isStyles ? STYLE_TOKEN_LABEL : TEMPLATE_TOKEN_LABEL}: {current.name}]
              </code>
            </div>
          )}

          {currentStyle && (
            <>
              {styleHtmlQuery.isLoading && (
                <div className="text-sm text-muted">
                  {i18nT('apps.pptxMaker.libraryPanel.loading')}
                </div>
              )}
              {styleHtmlQuery.data && (
                <BoardFrame
                  html={styleHtmlQuery.data.fullHtml}
                  title={i18nT('apps.pptxMaker.libraryPanel.style_preview')}
                />
              )}
            </>
          )}

          {currentTemplate && (
            <div className="flex flex-col gap-2">
              {currentTemplate.theme_colors?.background && (
                <div
                  className="rounded-lg h-24 flex items-center justify-between px-4 border border-border"
                  style={{ background: currentTemplate.theme_colors.background }}
                >
                  <span
                    className="text-lg"
                    style={{ color: currentTemplate.theme_colors.text || '#fff' }}
                  >
                    {FONT_SPECIMEN}
                  </span>
                  <div className="flex gap-1.5">
                    {templateAccents(currentTemplate.theme_colors).map((color) => (
                      <span
                        key={color}
                        className="w-4 h-4 rounded-full"
                        style={{ background: color }}
                      />
                    ))}
                  </div>
                </div>
              )}
              {(currentTemplate.fonts?.halfwidth || currentTemplate.fonts?.fullwidth) && (
                <div className="text-[13px] text-muted">
                  {i18nT('apps.pptxMaker.libraryPanel.fonts', {
                    fonts: [currentTemplate.fonts?.halfwidth, currentTemplate.fonts?.fullwidth]
                      .filter(Boolean)
                      .join(' / '),
                  })}
                </div>
              )}
              <div className="text-[13px] text-muted">
                {i18nT('apps.pptxMaker.libraryPanel.layouts', {
                  count: currentTemplate.layout_count ?? 0,
                })}
              </div>
              {currentTemplate.description && (
                <div className="text-[13px] text-text">{currentTemplate.description}</div>
              )}
            </div>
          )}
        </div>
      </div>
    </Card>
  )
}
