# KiroCrew Dependencies

All dependencies needed to build, install, and run KiroCrew. The backend is
distributed via `pip`; the frontend is built with `npm` + Vite. Memory and the
knowledge library use a local [Ollama](https://ollama.com) embedding server.

## System Requirements

| Requirement | Notes |
|---|---|
| **OS** | macOS or Linux (Windows is not supported by the `kiro-cli` backend) |
| **Python** | ≥ 3.9 |
| **Node.js + npm** | ≥ 18 (frontend build only) |
| **LLM backend** | `kiro-cli` over ACP — the only provider (`agent.provider = acp`) |
| **Ollama** | For memory + knowledge-library embeddings (`http://localhost:11434`) |

## Backend (pip)

Install the backend with:

```bash
pip install .                 # from a clone (build the frontend first)
pip install -e ".[voice]"     # editable dev install with optional voice extras
```

### Required (`install_requires`)

These are installed automatically by `pip`:

| Package | Constraint | Purpose |
|---|---|---|
| `aiohttp` | `>=3.9,<4` | Dashboard HTTP server and async HTTP client |
| `slack-sdk` | `>=3.27,<4` | Slack Socket Mode gateway |
| `websockets` | `>=12,<16` | Dashboard live updates / streaming |
| `croniter` | `>=2.0,<3` | Cron schedule parsing |
| `cron-descriptor` | `>=1.4,<2` | Human-readable cron descriptions |
| `numpy` | `>=1.21,<2` | Embedding vector math |
| `snowballstemmer` | `>=1.0` | Knowledge-library text stemming |
| `python-docx` | `>=0.8.6,<2` | Document ingestion (.docx) |
| `requests` | `>=2.28,<3` | Synchronous HTTP (Ollama, misc) |
| `PyYAML` | `>=6,<7` | Agent frontmatter, eval specs, task planner |
| `pysqlite3-binary` | `>=0.5.4` (Linux x86_64 only) | FTS5/UPSERT on older SQLite; not needed on macOS/aarch64/modern Linux |

### Optional extras

Install on demand:

```bash
pip install "kirocrew[voice]"   # speech-to-text (transcription)
pip install "kirocrew[aws]"     # AWS integrations (e.g. Bedrock provider)
```

| Extra | Packages | Purpose |
|---|---|---|
| `voice` | `boto3>=1.34,<2`, `amazon-transcribe>=0.6,<1` | Speech-to-text transcription |
| `aws` | `boto3>=1.34,<2` | AWS integrations (Bedrock provider, AWS-backed embeddings) |

> Core KiroCrew runs **without** these extras. `boto3` / `amazon-transcribe` are
> only imported by optional features and are intentionally kept out of the
> required dependencies.

### Console scripts

Installed onto `PATH` by `pip`:

| Command | Entry point |
|---|---|
| `kirocrew` | `kiro_crew.cli:main` |
| `kirocrew-browse` | `kiro_crew.browser.cli:main` |

## Frontend (npm)

The React SPA in `website/` is built with Vite. Install and build:

```bash
cd website
npm install        # uses the public npm registry (registry.npmjs.org)
npm run build      # tsc + vite build → website/dist
```

Production builds are copied into `src/kiro_crew/static/dist/` and served by the
backend.

### Runtime dependencies (highlights)

| Package | Purpose |
|---|---|
| `react`, `react-dom` | UI framework |
| `react-router-dom` | Client-side routing |
| `@reduxjs/toolkit`, `react-redux` | State management |
| `@tanstack/react-query` | Server-state / data fetching |
| `@monaco-editor/react`, `monaco-editor` | Code editor |
| `@xterm/xterm` + addons | Terminal view |
| `react-markdown`, `remark-gfm`, `remark-math`, `rehype-katex`, `rehype-raw`, `katex` | Markdown + math rendering |
| `highlight.js` | Syntax highlighting |
| `mermaid` | Diagram rendering |
| `d3`, `vis-network`, `vis-data` | Graph / memory visualizations |
| `@dnd-kit/*` | Drag-and-drop |
| `framer-motion` | Animations |
| `dompurify` | HTML sanitization |
| `lucide-react` | Icons |
| `tailwind-merge` | Tailwind class merging |
| `vscode-jsonrpc` | Gateway JSON-RPC transport |

### Dev / build dependencies (highlights)

`typescript`, `vite`, `@vitejs/plugin-react`, `vitest` + `@vitest/coverage-istanbul`,
`@testing-library/*`, `@playwright/test`, `msw`, `eslint` + plugins,
`tailwindcss` + `postcss` + `autoprefixer`, `fast-check`, `jscpd`, `jsdom`.

See `website/package.json` for the complete, version-pinned list.

## Install Order

```
1. git clone https://github.com/kirodotdev/KiroCrew.git
2. cd website && npm install && npm run build         # build frontend
3. cp -r website/dist src/kiro_crew/static/dist        # bundle into package
4. pip install -e ".[voice]"                           # install backend + scripts
5. install an LLM backend on PATH (claude-agent-acp / kiro-cli / Bedrock)
6. install Ollama + `ollama pull qwen3-embedding:0.6b` # embeddings
7. kirocrew setup                                      # config + credentials
8. kirocrew doctor && kirocrew gateway                 # verify + run
```

## Embedding Model

| Setting | Default | Notes |
|---|---|---|
| `memory.embedding_model` | `qwen3-embedding:0.6b` | Pull with `ollama pull qwen3-embedding:0.6b` |
| (documented fallback) | `nomic-embed-text` | dim 768; `ollama pull nomic-embed-text` |
| `memory.embedding_url` | `http://localhost:11434` | Local Ollama server |

## License Summary

All dependencies use permissive open-source licenses compatible with Apache-2.0.

### Backend (Python)

| Package | License |
|---|---|
| `aiohttp` | Apache-2.0 |
| `slack-sdk` | BSD-3-Clause |
| `websockets` | BSD-3-Clause |
| `croniter` | MIT |
| `cron-descriptor` | MIT |
| `numpy` | BSD-3-Clause |
| `snowballstemmer` | BSD-3-Clause |
| `pysqlite3-binary` | Zlib |
| `python-docx` | MIT |
| `requests` | Apache-2.0 |
| `PyYAML` | MIT |
| `boto3` (optional) | Apache-2.0 |
| `amazon-transcribe` (optional) | Apache-2.0 |

### Frontend (npm) — 299 production packages

| License | Count |
|---|---|
| MIT | 240 |
| ISC | 36 |
| BSD-3-Clause | 7 |
| Apache-2.0 | 6 |
| Apache-2.0 OR MIT | 4 |
| MPL-2.0 OR Apache-2.0 | 2 (Apache-2.0 chosen) |
| BSD-2-Clause | 1 |
| Unlicense | 1 |
| 0BSD | 1 |
| UNKNOWN | 1 (khroma — see note below) |

For dual-licensed packages, we choose the permissive option (Apache-2.0 or MIT).

## Platform Notes

- **macOS**: Fully supported.
- **Linux x86_64**: Fully supported. `pysqlite3-binary` is auto-installed to
  provide FTS5/UPSERT on older system SQLite builds.
- **Linux ARM / aarch64**: Supported; ships with a recent SQLite, so no
  `pysqlite3-binary` is needed.
- **Windows**: Not supported (the `kiro-cli` backend runs only on macOS and
  Linux).
