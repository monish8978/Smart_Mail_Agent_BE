# Changelog

All notable changes to the Mail AI Automation Platform are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.1.0] - 2026-10-01

### Added
- **Centralized Documentation Hub**: Established `docs/` hub including:
  - [`docs/RUNBOOK.md`](docs/RUNBOOK.md): Incident triage, deep health probe interpretation, component recovery, and backups.
  - [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md): REST ingestion endpoints, connector management, and error schemas.
  - [`docs/DATABASE_SCHEMA.md`](docs/DATABASE_SCHEMA.md): Complete ERD diagram and 15+ table data dictionary.
  - [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md): Runtime tuning, Redis database partitioning, and concurrency reference.
- **Dynamic Frontend Runtime Configuration**: Added `window.__APP_CONFIG__` support in `core.ts`, `Inbox.tsx`, and `App.tsx` allowing zero-rebuild API target overrides and subpath deployment routing.
- **Automated Regression Test Suite**: Expanded test suite to 84 automated tests, adding Redis thread key normalization validation (`test_07_thread_key_normalization`).

### Changed
- **Storage Resiliency Alignment**: Renamed fallback vector storage directory from `chroma_db/` to `knowledge_fallback/` across backend modules and Docker volume mounts.
- **Unified Order Resolution**: Refactored `app/order_routes.py` to route order lookups exclusively through the Dynamic Connector Engine (`run_order_status_lookup`).
- **Pruned Production Dependencies**: Trinned `requirements.txt` from 23 to 17 packages, eliminating 6 heavy libraries (`mcp`, `fastmcp`, `langchain-mcp-adapters`, `langgraph`, `langchain-openai`, `yoyo-migrations`).
- **Frontend Optimization**: Removed `zustand` and stale `package-lock.json`, standardizing on `pnpm`.

### Removed
- **Dead LangGraph & MCP Stack**: Deleted `app/langgraph_agent.py`, `app/mcp_server.py`, and `tests/test_mcp_server.py`.
- **Legacy Request Handler**: Deleted `app/request_handler.py` and obsolete payload endpoints (`/insert-create_payload_ticket`, `/insert-payload_get_ticket`).
- **Orphan UI Components**: Deleted `LangGraphVisualizer.tsx`, `FluentSegmentedTiles.tsx`, and `ApproveRegistration.tsx`.
- **Obsolete Scratch Files**: Removed root `.Test_files/`, `qdrant_storage/` host duplicate, and `Email_Bot_Architecture.pdf`.

### Fixed
- **Redis Thread Key Duplication**: Fixed `_make_key` in `app/chat_history.py` to normalize pre-prefixed `th_` keys, preventing redundant `th_th_` cache keys.
- **Deployment Build Ownership**: Resolved root ownership blockage on `Smart_Mail_Agent_FE/dist` ensuring non-sudo automated builds.

---

## [2.0.0] - 2026-09-30

### Added
- **Autonomous Multi-Stage Pipeline**: 4-stage ReAct email execution engine (`filters`, `enricher`, `agent`, `evaluator`).
- **Deep Observability**: Deep dependency health probe endpoint (`GET /health`) checking MySQL, Redis, Qdrant, and PyTorch embedding service.
- **Multi-Provider LLM Tier**: Dynamic routing across Groq, OpenAI, Anthropic, Gemini, and Ollama with automated circuit breakers and token cost calculation.
- **Enterprise Threading**: RFC-822 message threading (`Message-ID`, `In-Reply-To`, `References`) with Redis multi-turn chat history.
