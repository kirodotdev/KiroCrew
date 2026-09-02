# Requirements Document

## Introduction

KiroCrew is an autonomous agent management layer that adds persistent memory, scheduled jobs, heartbeats, background subagents, self-learning, and multi-session orchestration on top of kiro-cli's native LLM capabilities. Today it serves as a powerful developer tool with a dashboard, Slack integration, CLI, multi-agent orchestration, and a plugin architecture in progress.

This spec captures the product vision, tenets, user experience principles, and roadmap milestones that will evolve KiroCrew into an enterprise-grade collaborative AI platform from Amazon. While KiroCrew might not become a consumer product for everyone on the planet, KiroCrew should be designed for professional enterprise users across all job families and AI knowledge levels, not only developers. A developer, a product manager, a program manager, a scientist, a manager, and a leadership should all open KiroCrew and immediately be productive.

The end goal: users open KiroCrew and work with a full, self-evolving, autonomous team of AI teammates — each with their own role, ultra-long-horizon memory, and context. The team learns from every interaction and task, builds persistent and shareable knowledge through documents and memory, and gets measurably better over time. Kirocrew should 100x productivity, run hundreds if not thousands of agents 24/7, redefining how enterprise professionals work.

KiroCrew learns from the best and evolves beyond. OpenClaw is built for hardcore tinkerers who love wiring things together from scratch. Claude Cowork is designed for the everyday masses — simple, approachable, consumer-friendly. KiroCrew sits in between: as easy to use as Claude Cowork, as powerful as OpenClaw, but designed for neither extreme. It's the enterprise professional's AI workspace — intuitive enough that a user with zero AI background can be productive in minutes, deep enough that a senior SDE can build autonomous agent teams that run for weeks.

This is a strategic and product spec. It guides both UX and architecture decisions for all current and future KiroCrew specs.

## Glossary

- **KiroCrew**: The autonomous agent management layer built on kiro-cli, providing persistent memory, cron jobs, subagent orchestration, skills, and a dashboard UI.
- **Casual_User**: A professional user of any job family (SDE, PM, TPM, scientist, SDM) who installs KiroCrew and expects it to work immediately without reading documentation or editing config files. Not necessarily technical — may have zero AI background.
- **Power_User**: A professional user who customizes agent definitions, memory stores, plugins, cron jobs, and multi-agent workflows to fit specialized workflows. May be deeply technical or simply experienced with KiroCrew.
- **Agent_Team**: A declarative composition of multiple KiroCrew agents that collaborate on tasks, with defined delegation patterns and shared or isolated memory. The team is self-evolving — it learns from completed tasks and interactions to improve its own performance over time.
- **Progressive_Disclosure**: A UX pattern where advanced features are hidden by default and revealed as the user demonstrates readiness or explicitly opts in.
- **Zero_Config_Experience**: The state where KiroCrew is fully functional after installation with no manual configuration — a default agent, default workspace, and default memory store are auto-created. Works for any job family, not just developers.
- **AI_Teammate**: The conceptual model where each KiroCrew agent is presented to the user as a team member with a name, role, memory, and area of responsibility — not as a chatbot or tool. Critically, an AI teammate is not a personal assistant for one person. Like a real colleague, it works with multiple people and other agents, learns each person's preferences and context, and adapts its behavior accordingly.
- **Self_Evolution**: The property of KiroCrew agents and teams that they learn from every interaction, task completion, correction, and document — becoming more effective over ultra-long time horizons without manual retraining.
- **Ultra_Long_Horizon_Memory**: Persistent knowledge that spans weeks, months, and years. Not limited to one person — an agent knows the preferences, context, and working styles of everyone it interacts with, human or AI. The memory model should mimic how humans actually work: we don't just remember things — we write documents, keep notes, use apps to organize knowledge, contacts, and workflows. KiroCrew's knowledge layer encompasses all of this: organic memory from interactions, structured documents and notes, and an extensible ecosystem of tools and integrations (an "app store" model) that agents use to organize and retrieve information. The specific memory architecture is intentionally left open — what matters is that it works like a real colleague's brain plus their entire desk, filing cabinet, and toolset.
- **Contributor**: A developer who builds features, plugins, or extensions for KiroCrew, guided by the tenets in this document.
- **Claude_Cowork**: Anthropic's collaborative AI product designed for the everyday masses — simple, consumer-friendly, approachable. KiroCrew targets the enterprise tier with deeper power and customizability.
- **OpenClaw**: The open-source tinkerer-first AI agent framework. Powerful and extensible but designed for hardcore users who enjoy wiring components together from scratch. KiroCrew learns from OpenClaw's depth but wraps it in enterprise-grade usability.
- **Onboarding_Flow**: The first-run experience that introduces KiroCrew's capabilities to a new user without requiring configuration.
- **Tenet**: A prioritized design principle that guides architectural and UX decisions when trade-offs arise. Higher-ranked tenets take precedence.

## Requirements

### Requirement 1: Product Vision Statement

**User Story:** As a KiroCrew contributor, I want a clear product vision statement, so that all feature work aligns toward a single north star.

#### Acceptance Criteria

1. THE KiroCrew project SHALL adopt the following vision statement: "KiroCrew is the enterprise AI workspace where professionals across every job family work with a self-evolving team of AI teammates. It delivers 100x productivity through autonomous agent teams with ultra-long-horizon memory that learn from every interaction, build persistent shareable knowledge, and get better every day. It just works for everyone — from a PM who has never touched AI to an SDE who wants full control."
2. THE vision statement SHALL be referenced in all future spec introductions to ensure alignment.
3. THE vision statement SHALL position KiroCrew as Amazon's enterprise-grade answer to collaborative AI products such as Claude_Cowork — not a consumer product, but a professional tool for all job families and AI knowledge levels.

### Requirement 2: Design Tenets (Ordered by Priority)

**User Story:** As a KiroCrew contributor, I want an ordered set of design tenets, so that I can resolve trade-offs consistently when building features.

#### Acceptance Criteria

1. THE KiroCrew project SHALL adopt the following tenets in priority order, where higher-ranked tenets take precedence in trade-off decisions:

   - Tenet 1 — "It just works": THE KiroCrew system SHALL prioritize effortless usability above all other concerns. A professional user of any job family SHALL be able to install KiroCrew and have a productive conversation within 60 seconds without editing any config file, reading documentation, or understanding the agent model. No AI expertise required. Sensible defaults for everything, new features work with zero setup while offering overrides for power users.
   - Tenet 2 — "Simple to start, powerful in expert hands": THE KiroCrew system SHALL design every interface and capability to span all user levels through progressive disclosure. Beginners see a clean, simple experience. As users grow, advanced capabilities reveal themselves naturally through usage — not through documentation or config files. The floor is low, the ceiling is high, and the path between them is smooth.
   - Tenet 3 — "Teammates, not tools": THE KiroCrew system SHALL present AI agents as AI_Teammates with names, roles, memory, and areas of responsibility — not as chatbots, assistants, or tool interfaces. Every UX decision SHALL reinforce the mental model that the user is collaborating with a team of autonomous colleagues. And like any real team, teammates organize — specialists delegate to each other, collaborate on complex work, and compose into teams. THE system SHALL favor composing simple, well-defined agents into autonomous teams over building monolithic agents with many capabilities.
   - Tenet 4 — "The team never forgets": THE KiroCrew system SHALL treat knowledge — memory, learning, and self-evolution — as the core differentiator. Every interaction, task completion, correction, and document SHALL make the agent team more effective over ultra-long time horizons. Knowledge should mimic how humans actually work: not just recall, but documents, notes, apps, and tools that help organize information. The specific architecture is intentionally open, but knowledge quality, retrieval accuracy, and sharing SHALL be prioritized over adding new features. An extensible "app store" model SHALL allow agents to use specialized tools for organizing documents, contacts, workflows, and domain knowledge. The system learns and remembers — it does not just respond.
   - Tenet 5 — "Tailored, not siloed": THE KiroCrew system SHALL offer tailored experiences for different job families — an SDE, a PM, a scientist, and a manager should each feel like KiroCrew was built for them. But users SHALL never be locked into a lane. Boundaries between job-family experiences SHALL be soft — any user can cross into another domain's tools and workflows at any time. KiroCrew adapts to the user, not the other way around.
2. WHEN two tenets conflict in a design decision, THE Contributor SHALL favor the higher-ranked tenet and document the trade-off in the relevant spec or RFC.
3. THE tenets SHALL be included in the project's AGENTS.md or a dedicated TENETS.md file so that AI assistants and human contributors reference them during development.

### Requirement 3: Zero-Config First-Run Experience

**User Story:** As a casual user, I want KiroCrew to work immediately after installation with no setup steps, so that I can start getting value without friction.

#### Acceptance Criteria

1. WHEN a Casual_User runs KiroCrew for the first time, THE Onboarding_Flow SHALL auto-create a functional default setup without prompting the user for input.
2. THE Onboarding_Flow SHOULD offer an optional, lightweight step to indicate the user's job family or primary workflow (e.g., engineering, product, science, management) so that the default agent team, tools, and experience can be tailored — but this step SHALL be skippable with a sensible general-purpose default.
3. WHEN a Casual_User opens the KiroCrew dashboard for the first time, THE dashboard SHALL present a single chat interface with a welcome message that invites conversation — not a configuration wizard or settings page.
4. THE Zero_Config_Experience SHALL provide a functional agent with file system tools, memory, and basic skills enabled by default.
5. IF the Casual_User has not configured an embedding provider, THEN THE KiroCrew system SHALL operate in a degraded-but-functional mode where semantic memory is disabled and the user is informed via a non-blocking hint, not an error.
6. THE Onboarding_Flow SHALL introduce KiroCrew's capabilities (memory, cron, subagents, skills) through contextual hints during natural usage, not through an upfront tutorial.

### Requirement 4: Progressive Disclosure UX Model

**User Story:** As a KiroCrew user, I want advanced features to appear naturally as I grow, so that the interface stays simple when I need simple and powerful when I need power.

#### Acceptance Criteria

1. THE KiroCrew dashboard SHALL organize features into three disclosure tiers:
   - Tier 1 (Always visible): Chat, agent selector, basic memory view.
   - Tier 2 (Revealed on use): Cron jobs, skills, workspace management, session history.
   - Tier 3 (Expert): Agent team composition, plugin configuration, memory store management, config schema editor.
2. WHEN a Casual_User has not used any Tier 2 features, THE dashboard sidebar SHALL show only Tier 1 navigation items.
3. WHEN a user first uses a Tier 2 feature (e.g., creates a cron job via chat), THE dashboard SHALL reveal the corresponding navigation item with a subtle animation or badge.
4. WHEN a Power_User enables "Show all features" in settings, THE dashboard SHALL display all tiers permanently.
5. THE progressive disclosure model SHALL apply to the CLI as well: `kirocrew --help` SHALL show only essential commands by default, with `kirocrew --help-all` revealing the full command set.

### Requirement 5: AI Teammate Mental Model

**User Story:** As a KiroCrew user, I want each agent to feel like a real colleague on my team — one that works with multiple people, remembers everyone's context, and uses tools to stay organized.

#### Acceptance Criteria

1. THE KiroCrew agent definition SHALL support a `role` field (str) describing the agent's area of responsibility in human-readable terms (e.g., "oncall engineer", "code reviewer", "documentation writer").
2. THE KiroCrew agent definition SHALL support an `avatar` field (str) for a display icon or emoji that visually distinguishes the agent in the UI.
3. WHEN an AI_Teammate responds in the dashboard, THE chat interface SHALL display the agent's name and avatar alongside the response, reinforcing the teammate identity.
4. WHEN multiple agents collaborate on a task via Agent_Team composition, THE dashboard SHALL show which teammate is currently working and what each teammate contributed, using a threaded or attributed view.
5. THE default agent created during Zero_Config_Experience SHALL have a friendly name (e.g., "KiroCrew"), a role of "general assistant", and a default avatar.
6. AN AI_Teammate SHALL NOT be limited to serving a single user. Like a real colleague, an agent SHALL be capable of working with multiple people and other agents, learning each person's preferences, context, and working style over time.
7. AN AI_Teammate SHALL use documents, notes, and extensible tools (not just memory) to organize and retrieve knowledge — mirroring how real professionals use apps, filing systems, and collaboration tools alongside their own recall.

### Requirement 6: Competitive Positioning — The Enterprise Middle Ground

**User Story:** As a KiroCrew product owner, I want clear competitive positioning in the AI workspace landscape, so that contributors understand where KiroCrew sits relative to OpenClaw and Claude Cowork and what to prioritize.

#### Acceptance Criteria

1. THE KiroCrew project SHALL adopt the following competitive positioning framework:
   - **OpenClaw** occupies the "tinkerer" end of the spectrum — maximum power and extensibility, designed for hardcore users who enjoy assembling components from scratch. High ceiling, high floor.
   - **Claude Cowork** occupies the "consumer" end — maximum simplicity, designed for the everyday masses. Low floor, moderate ceiling.
   - **KiroCrew** occupies the enterprise middle ground — as easy to use as Claude Cowork, as powerful as OpenClaw, but designed for professional users across all job families. Low floor, high ceiling.
2. THE KiroCrew roadmap SHALL learn from both competitors: adopt OpenClaw's depth in agent composition, plugin extensibility, and autonomous workflows; adopt Claude Cowork's polish in onboarding, UX simplicity, and zero-config experience. Then evolve beyond both through self-evolving agent teams, ultra-long-horizon memory, and enterprise-grade integration.
3. THE KiroCrew roadmap SHALL target parity with Claude_Cowork on: multi-turn collaborative chat, persistent context across sessions, file and code awareness, and task delegation to background workers.
4. THE KiroCrew roadmap SHALL differentiate through: self-evolving autonomous agent teams, ultra-long-horizon persistent knowledge (memory + documents + notes + extensible tools), multi-person agent model (agents serve teams, not individuals), an "app store" ecosystem for agent tools and integrations, declarative multi-agent teams with delegation patterns, a plugin architecture for backends and providers, cross-job-family accessibility (not developer-only), and deep integration with team tooling (Slack, MCP tool servers, issue trackers).
5. THE KiroCrew system SHALL support a local-first architecture where all data (memory, config, history) lives on the user's machine by default, with opt-in remote backends via the plugin system.
6. THE KiroCrew system SHALL support multiple simultaneous chat sessions (slots) with independent agent and workspace bindings, enabling parallel workflows.
7. THE KiroCrew system SHALL target enterprise professionals as its primary audience — not consumers, not tinkerers. The product is designed for users who need 100x productivity in their professional work, across all job families (engineering, product, science, management) and all levels of AI familiarity.

### Requirement 7: Contributor Guidance — Architecture Decision Framework

**User Story:** As a KiroCrew contributor, I want clear guidance on how to make architecture decisions, so that the codebase evolves consistently toward the vision.

#### Acceptance Criteria

1. WHEN a Contributor adds a new user-facing feature, THE feature SHALL include a zero-config default that works without any user action (Tenet 1).
2. WHEN a Contributor adds a new config field, THE field SHALL have a sensible default value and the system SHALL behave identically to before the field existed when the field is absent (Tenet 1).
3. WHEN a Contributor designs a new UI surface, THE surface SHALL follow the progressive disclosure model: simple by default, powerful on demand (Tenet 2).
4. WHEN a Contributor designs a complex workflow, THE Contributor SHALL prefer composing multiple simple agents over building a single complex agent (Tenet 3).
5. WHEN a Contributor faces a trade-off between ease of use and power, THE Contributor SHALL choose ease of use and provide a power-user escape hatch (Tenet 1 and Tenet 2).
6. WHEN a Contributor designs a feature for a specific job family, THE feature SHALL be accessible to users outside that job family (Tenet 5).
7. THE Contributor guidance SHALL be documented alongside the tenets so that code reviewers can reference specific tenets when reviewing pull requests.

### Requirement 8: Roadmap Milestones — From Current State to AI Team Vision

**User Story:** As a KiroCrew product owner, I want defined roadmap milestones, so that the team can plan and track progress toward the full AI team vision.

#### Acceptance Criteria

1. THE KiroCrew roadmap SHALL define the following milestones in order:
   - Milestone 1 — "Solid Foundation" (current): Complete config-schema, agent-workspace-bindings, multi-agent-orchestration, workspace-crud, and inline-tool-cards specs. Stabilize the agent resolver, memory consolidation, and dashboard UX.
   - Milestone 2 — "Memory Isolation": Implement named memory stores at runtime (RFC Phase 3). Each agent gets its own memory boundary. Lessons remain global.
   - Milestone 3 — "Plugin Ecosystem": Implement the plugin system (RFC Phase 4). Swappable memory backends and embedding providers. Entry-point-based discovery.
   - Milestone 4 — "Agent Teams": Implement multi-agent composition (RFC Phase 5). Declarative agent teams with delegation patterns. Agents can delegate subtasks to other agents.
   - Milestone 5 — "Progressive UX": Implement the progressive disclosure model across dashboard and CLI. Tiered navigation. Contextual feature revelation. Onboarding flow.
   - Milestone 6 — "AI Teammates": Implement the full teammate mental model. Agent roles, avatars, attributed responses, team activity views. The user sees a team, not a tool.
   - Milestone 7 — "Cowork Parity": Feature parity with Claude_Cowork on collaborative workflows. Shared context, real-time collaboration hints, project-level awareness.
2. EACH milestone SHALL be achievable independently, with no milestone requiring completion of a later milestone.
3. EACH milestone SHALL build on the previous milestone's capabilities, creating a cumulative product experience.
4. THE roadmap SHALL align with the existing RFC phases (3–7) for technical milestones while adding product and UX milestones that the RFC does not cover.

### Requirement 9: UX Principle — Simplicity at Every Layer

**User Story:** As a KiroCrew user, I want every interaction surface (dashboard, CLI, Slack, config) to feel simple and consistent, so that I never feel overwhelmed regardless of which interface I use.

#### Acceptance Criteria

1. THE KiroCrew dashboard SHALL maintain a maximum of 7 top-level navigation items visible at any time, regardless of how many features are installed (progressive disclosure manages overflow).
2. THE KiroCrew CLI SHALL present a flat command structure for common operations (`kirocrew chat`, `kirocrew agent`, `kirocrew cron`) with subcommands for less common operations.
3. THE KiroCrew Slack integration SHALL respond to natural language without requiring slash commands or structured syntax for common operations.
4. THE KiroCrew config file SHALL remain a single `config.json` with a flat-ish structure — deeply nested config is a sign that the feature needs better defaults or should be a plugin.
5. WHEN a feature adds complexity to one interaction surface, THE feature SHALL not add complexity to other surfaces unless the user explicitly opts in.

### Requirement 10: Quality Bar — What Ships and What Doesn't

**User Story:** As a KiroCrew contributor, I want a clear quality bar for shipping features, so that the product maintains its "it just works" reputation.

#### Acceptance Criteria

1. THE KiroCrew project SHALL require the following before any feature ships:
   - The feature works with zero configuration on a fresh install.
   - The feature does not break any existing `config.json`.
   - The feature has unit tests covering the happy path and at least one error path.
   - The feature has been tested on both macOS and Linux.
   - The feature's UI follows the existing dashboard layout guide and component library.
2. IF a feature cannot meet the zero-config requirement, THEN THE feature SHALL ship as an opt-in experimental feature behind a config flag, with a clear path to becoming zero-config in a future milestone.
3. THE KiroCrew project SHALL maintain a "works out of the box" integration test that installs KiroCrew from scratch and verifies a basic chat interaction completes successfully without any manual configuration.
