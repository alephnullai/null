# Null Nebula

3D galaxy visualization of Null Memory. Each point is a fact, mistake, or decision; each color a personality; each pulse a memory firing.

## Quick start

In one terminal, boot the Python backend:

```bash
cd null
pip install -e ".[nebula]"
null nebula serve
```

In another, dev the frontend:

```bash
cd nebula-web
npm install
npm run dev
```

Open <http://localhost:5173>. The page proxies `/nebula/*` to the backend on 8787.

## Architecture

- **Backend** — FastAPI (`src/null_memory/nebula/server.py`) + UMAP/HDBSCAN projector. Reads `~/.null/unified.db`. Endpoints: `/nebula/snapshot`, `/nebula/identity`, `/nebula/fact/{id}`, `/nebula/meta`, `ws /nebula/events`.
- **Frontend** — React + Vite + Three.js via `@react-three/fiber` + `drei`. Zustand for state.
- **Data** — UMAP projects each fact's 384-d embedding to 3D, HDBSCAN clusters. Coordinates cached on `facts.viz_{x,y,z}` and `facts.cluster_id` (schema v14).

## Color language

| Signal | Color |
|---|---|
| Atlas | cyan (`#00d4ff`) |
| Cybil | amber (`#ffb020`) |
| Mercury | coral (`#ff7a5c`) |
| Logos | violet (`#b080ff`) |
| Shared truth (≥2 personalities) | white-silver (`#e8e8f0`) |
| Mistakes | red (`#ff3a5c`) |

Anchors: 2× size, breathe slowly. Identity center: dynamic blend of recent activity.

## Production build

```bash
cd nebula-web
npm run build
```

Output at `dist/`. The FastAPI backend auto-serves `dist/` at `/` when present.

## Remote deployment (future)

All endpoints are auth-wrappable via FastAPI middleware. Future `nebula.alephnull.ai` deployment adds JWT middleware + per-user DB scoping. No schema retrofit.
