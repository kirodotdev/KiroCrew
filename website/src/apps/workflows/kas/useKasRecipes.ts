/**
 * Recipe data hooks for the Kiro Agent Workflows panel (lane L4).
 *
 * These live apart from `useKasWorkflows.ts` (the run/state hooks) so the
 * recipe list can do three things that file does not: follow KAS's
 * `recipes_changed` push live, offer a manual Refresh, and create / delete
 * recipe files. They REUSE `useKasWorkflows`'s query key (`kasRecipesKey`) and
 * state poll, so there is still one recipe cache and one state poll — this file
 * only adds the invalidation and the mutations around them.
 */
import { useEffect, useRef } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../../../api/client'
import type { KasRecipe } from '../../../api/client'
import { kasRecipesKey, useKasState } from './useKasWorkflows'

export const kasRecipeDetailKey = (cwd: string, source: string) =>
  ['workflows', 'kas', 'recipeDetail', cwd, source] as const

export interface KasRecipesResult {
  recipes: KasRecipe[]
  isLoading: boolean
  error: unknown
  /** Re-read the recipe list now (the Refresh button). */
  refresh: () => void
}

/**
 * The recipes KAS lists for `cwd`, re-read whenever KAS reports the set on disk
 * changed. The state poll carries a `recipesRevision` that the gateway bumps on
 * every `recipes_changed` push; when it moves, this invalidates the recipe
 * query so the list re-reads without a human refresh (gap 16). `refresh`
 * forces the same re-read on demand.
 */
export function useKasRecipesList(cwd: string, enabled: boolean): KasRecipesResult {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: kasRecipesKey(cwd),
    queryFn: () => api.kasRecipes(cwd),
    enabled: enabled && !!cwd,
    staleTime: 30_000,
  })

  // Follow KAS's `recipes_changed` push: the shared state poll surfaces the
  // revision, so a change to the recipe set on disk re-reads the list here.
  const state = useKasState(enabled && !!cwd)
  const revision = state.data?.recipesRevision
  const lastRevision = useRef<number | undefined>(undefined)
  useEffect(() => {
    if (revision === undefined) return
    if (lastRevision.current === undefined) {
      lastRevision.current = revision
      return
    }
    if (revision !== lastRevision.current) {
      lastRevision.current = revision
      if (cwd) void queryClient.invalidateQueries({ queryKey: kasRecipesKey(cwd) })
    }
  }, [revision, cwd, queryClient])

  return {
    recipes: query.data?.recipes ?? [],
    isLoading: query.isLoading,
    error: query.error,
    refresh: () => {
      if (cwd) void queryClient.invalidateQueries({ queryKey: kasRecipesKey(cwd) })
    },
  }
}

/**
 * One recipe's steps (prompt text + agent), for the detail view (gap 27). A
 * bundled or YAML recipe answers `definitionAvailable: false` with no steps.
 * Read once and cached per (project, source); only requested for a launchable
 * recipe, since a sourceless one has nothing to read.
 */
export function useKasRecipeDetail(cwd: string, source: string | null, enabled: boolean) {
  return useQuery({
    queryKey: kasRecipeDetailKey(cwd, source ?? ''),
    queryFn: () => api.kasRecipeDetail(cwd, source as string),
    enabled: enabled && !!cwd && !!source,
    staleTime: 30_000,
    retry: false,
  })
}

/** Create / delete recipe files, each re-reading the list on success. */
export function useKasRecipeMutations(cwd: string) {
  const queryClient = useQueryClient()
  const invalidate = () => {
    if (cwd) void queryClient.invalidateQueries({ queryKey: kasRecipesKey(cwd) })
  }
  const create = useMutation({
    mutationFn: ({ name, scope }: { name: string; scope: 'workspace' | 'global' }) =>
      api.kasCreateRecipe(cwd, name, scope),
    onSuccess: invalidate,
  })
  const remove = useMutation({
    mutationFn: ({ source }: { source: string }) => api.kasDeleteRecipe(cwd, source),
    onSuccess: invalidate,
  })
  return { create, remove }
}
