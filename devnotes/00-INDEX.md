# Robomuffin Idea Factory — Dev Notes Index

Reference documentation for bootstrapping new Klein/ComfyUI-based generation projects.
These notes capture the architecture, patterns, and lessons learned from the RBMN Storyboard App.

## Documents

1. **[ComfyUI Integration](01-comfyui-integration.md)** — HTTP/WS client, workflow JSON prep, multi-server dispatch, worker selection, job lifecycle, output retrieval, error handling
2. **[LLM Prompt Enhancement](02-llm-prompt-enhancement.md)** — System prompts, context assembly, provider abstraction, two-pass logic, video flow generation, Base on Lyrics
3. **[Settings & API Architecture](03-settings-api-architecture.md)** — AppSettings model, API endpoints, generation request flow, job queue, SSE streaming, patterns for adding new settings
4. **[Frontend Patterns](04-frontend-patterns.md)** — API client, TypeScript types, Zustand store, react-query usage, Settings page UI patterns, SSE consumption
5. **[Workflow JSON Reference](05-workflow-json-reference.md)** — Klein and LTX workflow structures, node titles, field mappings, dynamic workflow system
6. **[Quick Start Checklist](06-quick-start-checklist.md)** — Step-by-step to set up a new Klein-based project from scratch
