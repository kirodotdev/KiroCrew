import { useMemo, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import type { NavigateFunction } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'

import { useAppDispatch, useAppSelector } from '../../store'
import { createSlot, setPendingInput } from '../../store/chatSlice'

/**
 * Shared §2 Enter-matrix engine for the Search Everywhere command palette
 * (Mesh-2151, step 17).
 *
 * The palette has two classes of result. **Invokable** results (Skills,
 * Prompts) drop a `$skill` / `@prompt` *token* into a chat. **Navigable**
 * results (Sessions, Knowledge, Pages, Actions) move you somewhere or run a
 * command. This module owns the invokable half — the context-aware
 * insert-vs-new-session decision — so Skills and Prompts share one
 * implementation instead of duplicating it (jscpd duplication gate).
 *
 * ## Security (BSC1 — Input Validation, ARCC `cnt_u7sfVeParu7OlX`)
 *
 * Invokable activation MUST route through the **existing shipped allowlist
 * resolvers**, never a second FE-side resolution path, and MUST NOT build any
 * filesystem path from user input. Concretely, the only thing this module ever
 * does with a skill / prompt name is splice it into a plain token string
 * (`$<name>` / `@<fullName>`) and hand that string to the existing chat-submit
 * path via `setPendingInput` (insert into the current composer) or
 * `createSlot` + `setPendingInput` (seed a new session's composer). Resolution
 * then happens server-side on submit, exactly as it does for the inline
 * `$` / `@` pickers:
 *
 *  - `$skill`  → `SkillsLoader.get_triggered_skills()` / `load_skill()`, which
 *    is guarded by `_safe_name()` (rejects `..` / `\\`) and `validate_file_path()`
 *    (`src/kiro_crew/skills.py`).
 *  - `@prompt` → `_expand_prompt_mention()` on the submit path
 *    (`src/kiro_crew/dashboard/chat_runner.py:784`, consumed at lines 999/1201).
 *
 * Those resolvers already apply `redact_credentials()` / `redact_exfiltration_urls()`.
 * Because the FE only ever emits a token string, there is no new untrusted
 * input sink here and no filesystem access from user-controlled values.
 */

/** The two ways an invokable token can reach a chat. */
export interface InvokableTokenSinks {
  /** Insert the token into the active session's composer. */
  insertToken: (token: string) => void
  /** Open a new session and seed its composer with the token. */
  newSessionWithToken: (token: string) => void
}

/**
 * Pure §2 Enter rule for an invokable (Skill / Prompt) token. Returns the
 * callback that primary Enter should run:
 *
 *  - active chat present → insert the token into the current composer
 *  - no active chat      → open a new session seeded with the token
 *
 * No React, no I/O — exported standalone so the matrix decision is
 * unit-testable without mounting a provider or the React tree.
 */
export function resolveInvokableEnter(
  hasActiveChat: boolean,
  token: string,
  sinks: InvokableTokenSinks,
): () => void {
  return hasActiveChat
    ? () => sinks.insertToken(token)
    : () => sinks.newSessionWithToken(token)
}

/**
 * Live palette actions wired to the chat store + router. Providers call this in
 * their hook and use it to build their result activations per the §2 matrix.
 *
 * `newSessionWithToken` mirrors the established FE pattern
 * (`dispatch(createSlot(...)).unwrap().then(...)`, see `ChatSidebar` /
 * `ChatPage`): `createSlot` sets the new slot active, so seeding
 * `pendingInput` afterwards lands the token in the *new* session's composer.
 */
export interface PaletteActions {
  /** Whether a chat session is currently active. */
  hasActiveChat: boolean
  /** Insert a token into the active composer (`setPendingInput`). */
  insertToken: (token: string) => void
  /** Open a new session and seed its composer with the token. */
  newSessionWithToken: (token: string) => void
  /**
   * Primary-Enter convenience: insert into the active chat, or open a new
   * session when there is none. Thin wrapper over {@link resolveInvokableEnter}
   * so providers don't re-implement the matrix branch.
   */
  enterInsertOrNewSession: (token: string) => void
  /** Navigate to an in-app route (open / preview surfaces). */
  navigate: (route: string) => void
}

export function usePaletteActions(): PaletteActions {
  const dispatch = useAppDispatch()
  const navigate: NavigateFunction = useNavigate()
  const hasActiveChat = useAppSelector((s) => s.chat.activeSlot !== null)

  const { mutate: doCreateSlot } = useMutation({
    mutationFn: () => dispatch(createSlot(undefined)).unwrap(),
    onError: (err: unknown) => { console.error('[palette] failed to create session:', err) },
  })

  const insertToken = useCallback((token: string) => {
    dispatch(setPendingInput(token))
  }, [dispatch])

  const newSessionWithToken = useCallback((token: string) => {
    doCreateSlot(undefined, {
      onSuccess: () => { dispatch(setPendingInput(token)); navigate('/chat') },
    })
  }, [doCreateSlot, dispatch, navigate])

  return useMemo<PaletteActions>(() => ({
    hasActiveChat,
    insertToken,
    newSessionWithToken,
    enterInsertOrNewSession: (token: string) =>
      resolveInvokableEnter(hasActiveChat, token, { insertToken, newSessionWithToken })(),
    navigate: (route: string) => navigate(route),
  }), [hasActiveChat, insertToken, newSessionWithToken, navigate])
}
