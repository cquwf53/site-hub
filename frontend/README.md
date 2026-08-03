# SiteHub frontend

Vue 3 + TypeScript + Vite desktop-first frontend. It is an npm workspace of the repository root.

```powershell
npm install --workspace frontend
npm run typecheck --workspace frontend
npm run test --workspace frontend
npm run build --workspace frontend
```

During development, Vite proxies `/api` to `http://127.0.0.1:8686`. Production requests remain same-origin.

The client bootstraps a CSRF token through `GET /api/v1/bootstrap`. Mutating requests send `X-CSRF-Token`. Long operations consume `TaskRef { jobId, status, deduplicated }`, subscribe to `/api/v1/stream`, and fall back to polling the referenced job. Adapter configuration is rendered from `/api/v1/adapters`; secret values are write-only and only configuration state is displayed.
