/**
 * The built-in auto-research app's API (/api/apps/auto-research): draft
 * validation and grill expansion, campaign list/detail/create/actions/delete,
 * the grill tree, nudges and questions, and the knowledge, artifact and report
 * exports.
 */

import type { ClientTransport } from './transport'

export function createAutoResearchEndpoints({ get, post, del, patch, j }: ClientTransport) {
  const drafts = {
    // Auto-research
    researchValidate: (body: object) => post("/api/apps/auto-research/validate", body).then(j),
    researchGrillExpand: (body: object) => post("/api/apps/auto-research/grill/expand", body).then(j),
  }

  const campaigns = {
    researchCampaigns: () => get("/api/apps/auto-research/campaigns").then(j),
    researchCampaign: (id: string) => get("/api/apps/auto-research/campaigns/" + id).then(j),
    researchCreate: (body: object) => post("/api/apps/auto-research/campaigns", body).then(j),
    researchAction: (id: string, action: string, body?: object) => patch("/api/apps/auto-research/campaigns/" + id, { action, ...body }).then(j),
    researchGrillTree: (id: string) => get("/api/apps/auto-research/campaigns/" + id + "/grill-tree").then(j),
    researchNudge: (id: string, text: string) => post("/api/apps/auto-research/campaigns/" + id + "/nudge", { text }).then(j),
    researchAddQuestion: (id: string, text: string) => post("/api/apps/auto-research/campaigns/" + id + "/questions", { text }).then(j),
    researchToKnowledge: (id: string) => post("/api/apps/auto-research/campaigns/" + id + "/to-knowledge", {}).then(j),
    researchKnowledgeStatus: (id: string) => get("/api/apps/auto-research/campaigns/" + id + "/knowledge-status").then(j),
    researchToArtifact: (id: string) => post("/api/apps/auto-research/campaigns/" + id + "/to-artifact", {}).then(j),
    researchReportStatus: (id: string) => get("/api/apps/auto-research/campaigns/" + id + "/report-status").then(j),
    researchReport: (id: string) => get("/api/apps/auto-research/campaigns/" + id + "/report").then(j),
    researchDelete: (id: string) => del("/api/apps/auto-research/campaigns/" + id).then(j),
  }

  return { drafts, campaigns }
}
