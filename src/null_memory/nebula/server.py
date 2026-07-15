"""Null Nebula — FastAPI backend.

Serves the 3D visualization frontend with:
  GET  /nebula/snapshot  → every active fact with coords + metadata
  GET  /nebula/identity  → center sphere state + cluster centroids
  GET  /nebula/fact/{id} → full detail for hover/click
  GET  /nebula/meta      → personality palette + stats
  WS   /nebula/events    → live firing events (recall/observe/etc)

Every /nebula/* route (HTTP + websocket) requires a per-launch bearer
token (``Authorization: Bearer <t>`` header or ``?token=`` query param).
The launcher prints the full URL including the token at startup. Set
NULL_NEBULA_NO_AUTH=1 to disable (dev escape hatch — warns loudly).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Body

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from null_memory.nebula.projector import (
    cluster_centroids,
    needs_reproject,
    project_all,
    REPROJECT_THRESHOLD,
)

DEFAULT_UNIFIED_PATH = os.path.expanduser("~/.null/unified.db")

# Core palette — the four Null-built-in personalities. User-defined
# personalities register their own colors via identity.json and get
# merged into this map at startup by palette_augments().
PERSONALITY_COLORS = {
    "atlas":   "#00d4ff",  # electric cyan — default conductor persona
    "cybil":   "#ffb020",  # amber
    "mercury": "#ff7a5c",  # coral
    "logos":   "#b080ff",  # violet
}

try:
    from null_memory.personality import palette_augments as _pal_augments
    PERSONALITY_COLORS.update(_pal_augments())
except Exception:
    # Personality loader failures are non-fatal for Nebula — the core
    # palette still works.
    pass
SHARED_COLOR = "#e8e8f0"     # white-silver — facts known by 2+ personalities
MISTAKE_COLOR = "#ff3a5c"    # shade of red (category, not special graphics)

# Poll interval for live events (seconds) — snappy for real-time feel
EVENT_POLL_SECONDS = 0.2


def _conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: float, g: float, b: float) -> str:
    return f"#{int(max(0, min(255, r))):02x}{int(max(0, min(255, g))):02x}{int(max(0, min(255, b))):02x}"


def _point_color(personalities: list[str]) -> str:
    """Base color for a point.

    - 0 personalities → shared silver (legacy fallback)
    - 1 personality  → that personality's palette color
    - 2+ personalities → BLENDED color (average RGB) so each combination
      gets its own distinct shade. atlas+mercury reads teal, atlas+logos
      reads periwinkle, etc. Makes shared clusters visually informative.
    """
    if not personalities:
        return SHARED_COLOR
    if len(personalities) == 1:
        return PERSONALITY_COLORS.get(
            personalities[0].lower(), PERSONALITY_COLORS["atlas"]
        )
    # Blend — average the RGB of each included personality.
    rgbs = []
    for p in personalities:
        hex_val = PERSONALITY_COLORS.get(p.lower())
        if hex_val:
            rgbs.append(_hex_to_rgb(hex_val))
    if not rgbs:
        return SHARED_COLOR
    r = sum(x[0] for x in rgbs) / len(rgbs)
    g = sum(x[1] for x in rgbs) / len(rgbs)
    b = sum(x[2] for x in rgbs) / len(rgbs)
    # Saturate a touch — blends average toward grey; boost chroma by 1.15×
    # relative to the mean luminance so combinations stay vivid.
    lum = (r + g + b) / 3
    r = lum + (r - lum) * 1.15
    g = lum + (g - lum) * 1.15
    b = lum + (b - lum) * 1.15
    return _rgb_to_hex(r, g, b)


def _recent_glow(personality: str | None) -> str | None:
    """Secondary glow color = most-recent accessor's personality color.
    Stacked over base color in the renderer."""
    if not personality:
        return None
    return PERSONALITY_COLORS.get(personality.lower())


def create_app(unified_path: str = DEFAULT_UNIFIED_PATH,
               cluster_labels: dict | None = None,
               port: int = 8787,
               token: str | None = None) -> FastAPI:
    """Build the FastAPI app. Keeps state in app.state so the launcher
    can refresh projections without restarting.

    A per-launch auth token guards every /nebula/* route. Pass ``token``
    to reuse one (tests); otherwise a fresh ``secrets.token_urlsafe(32)``
    is generated. NULL_NEBULA_NO_AUTH=1 disables auth entirely (dev
    escape hatch — off by default, warns loudly when active).
    """
    from null_memory.__version__ import __version__ as null_version
    boot_time = time.time()
    app = FastAPI(
        title="Null Nebula",
        description="3D galaxy visualization of Null Memory",
        version=null_version,
    )
    if os.environ.get("NULL_NEBULA_NO_AUTH") == "1":
        token = None
        print("=" * 64)
        print("  WARNING: NULL_NEBULA_NO_AUTH=1 — Nebula API auth DISABLED.")
        print("  Any local process or webpage can read your full memory.")
        print("=" * 64)
    elif token is None:
        token = secrets.token_urlsafe(32)
    app.state.auth_token = token

    # CORS — only the server's own origins. Memory contents must not be
    # fetchable by arbitrary webpages running in the same browser.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.unified_path = unified_path
    app.state.cluster_labels = cluster_labels or {}
    app.state.last_event_tick = time.time()

    def _token_ok(auth_header: str | None, query_token: str | None) -> bool:
        expected = app.state.auth_token
        if not expected:
            return True  # auth disabled via NULL_NEBULA_NO_AUTH
        supplied = None
        if auth_header and auth_header.lower().startswith("bearer "):
            supplied = auth_header[7:].strip()
        elif query_token:
            supplied = query_token
        return bool(supplied) and secrets.compare_digest(supplied, expected)

    @app.middleware("http")
    async def _require_token(request: Request, call_next):
        """401 every /nebula/* data/mutation route without the launch token.
        Static frontend assets (served at /) stay unauthenticated — they
        contain no memory data."""
        # CORS preflights carry no credentials by spec — let the CORS
        # layer answer them; the actual request still needs the token.
        if request.method == "OPTIONS":
            return await call_next(request)
        if request.url.path.startswith("/nebula/") and not _token_ok(
            request.headers.get("authorization"),
            request.query_params.get("token"),
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "missing or invalid token"},
            )
        return await call_next(request)

    # Auto-reproject on boot if many facts are stale
    missing = needs_reproject(unified_path)
    if missing >= REPROJECT_THRESHOLD:
        stats = project_all(unified_path, force=False)
        app.state.cluster_labels = stats.get("cluster_labels", {})

    # ── Endpoints ────────────────────────────────────────────────────────

    @app.get("/nebula/meta")
    def get_meta():
        conn = _conn(unified_path)
        try:
            total = conn.execute(
                """SELECT COUNT(*) FROM facts
                   WHERE archived=0 AND forgotten=0 AND superseded_by IS NULL
                         AND viz_x IS NOT NULL"""
            ).fetchone()[0]
            clusters = conn.execute(
                """SELECT COUNT(DISTINCT cluster_id) FROM facts
                   WHERE cluster_id IS NOT NULL AND cluster_id >= 0"""
            ).fetchone()[0]
            anchors = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE anchor_type IS NOT NULL"
            ).fetchone()[0]
            return {
                "palette": PERSONALITY_COLORS,
                "shared_color": SHARED_COLOR,
                "mistake_color": MISTAKE_COLOR,
                "total_points": total,
                "cluster_count": clusters,
                "anchor_count": anchors,
                "cluster_labels": app.state.cluster_labels,
                # Stale-detection: frontend compares these across polls
                # and toasts if the server was restarted.
                "server_version": null_version,
                "server_boot_time": boot_time,
            }
        finally:
            conn.close()

    @app.get("/nebula/snapshot")
    def get_snapshot(limit: int = 5000):
        """Every active fact with layout + metadata. Cap via limit."""
        conn = _conn(unified_path)
        try:
            rows = conn.execute(
                """
                SELECT
                  f.id, f.fact, f.tier, f.anchor_type, f.cluster_id,
                  f.viz_x, f.viz_y, f.viz_z,
                  f.last_accessed, f.access_count, f.confidence, f.impact,
                  f.project
                FROM facts f
                WHERE f.archived = 0 AND f.forgotten = 0
                  AND f.superseded_by IS NULL
                  AND f.viz_x IS NOT NULL
                ORDER BY f.confidence DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

            # Collect personality views per fact (batch for efficiency)
            fact_ids = [r["id"] for r in rows]
            pv_by_fact: dict[str, list[dict]] = {}
            if fact_ids:
                placeholders = ",".join("?" * len(fact_ids))
                pv_rows = conn.execute(
                    f"""SELECT fact_id, personality, last_accessed, access_count
                        FROM personality_views WHERE fact_id IN ({placeholders})""",
                    fact_ids,
                ).fetchall()
                for pv in pv_rows:
                    pv_by_fact.setdefault(pv["fact_id"], []).append(dict(pv))

            points = []
            for r in rows:
                views = pv_by_fact.get(r["id"], [])
                personalities = sorted({v["personality"] for v in views})
                # Primary = personality with highest access_count; if tie, lexicographic
                primary = None
                if views:
                    primary = max(
                        views,
                        key=lambda v: (v.get("access_count") or 0, v.get("last_accessed") or "")
                    )["personality"]
                # Recent accessor for glow overlay
                most_recent = None
                if views:
                    most_recent = max(
                        views,
                        key=lambda v: v.get("last_accessed") or ""
                    )["personality"]

                size = 1.0
                if r["anchor_type"]:
                    size = 2.0
                elif r["tier"] == "core":
                    size = 1.6
                elif r["tier"] == "durable":
                    size = 1.3
                elif r["tier"] == "ephemeral":
                    size = 0.8

                points.append({
                    "id": r["id"],
                    "x": r["viz_x"],
                    "y": r["viz_y"],
                    "z": r["viz_z"],
                    "cluster_id": r["cluster_id"],
                    "color": _point_color(personalities),
                    "glow": _recent_glow(most_recent),
                    "size": size,
                    "opacity": float(r["confidence"] or 0.5),
                    "anchor_type": r["anchor_type"],
                    "tier": r["tier"],
                    "personalities": personalities,
                    "last_accessed": r["last_accessed"],
                    "access_count": r["access_count"],
                    "project": r["project"],
                    "fact_preview": (r["fact"] or "")[:120],
                    "type": "fact",
                })

            # Phase 5.3 — mistakes as their own points in the galaxy.
            # Type-tagged so the frontend can render them with distinct
            # (dimmer, red-hued, smaller) aesthetics. Mistake ids in the
            # point array are prefixed with 'm_' to match the id format
            # nebula_events uses, so event->point lookup works.
            mistake_rows = conn.execute(
                """SELECT id, mistake, why, project, personality,
                          created_at, viz_x, viz_y, viz_z
                   FROM mistakes
                   WHERE viz_x IS NOT NULL"""
            ).fetchall()
            for mr in mistake_rows:
                points.append({
                    "id": f"m_{mr['id']}",
                    "x": mr["viz_x"],
                    "y": mr["viz_y"],
                    "z": mr["viz_z"],
                    "cluster_id": None,
                    "color": "#b32a3a",
                    "glow": None,
                    "size": 0.7,
                    "opacity": 0.55,
                    "anchor_type": None,
                    "tier": None,
                    "personalities": [mr["personality"]] if mr["personality"] else [],
                    "last_accessed": mr["created_at"],
                    "access_count": 0,
                    "project": mr["project"],
                    "fact_preview": f"MISTAKE: {(mr['mistake'] or '')[:100]}",
                    "type": "mistake",
                })
            return {"points": points, "count": len(points)}
        finally:
            conn.close()

    @app.get("/nebula/identity")
    def get_identity():
        """Central identity sphere state + cluster centroid connections.

        Identity color is dynamic — weighted blend of recent personality
        activity. Sphere pulses with drift stability.
        """
        conn = _conn(unified_path)
        try:
            # Recent activity across personalities for color blending
            row = conn.execute(
                """SELECT personality, COUNT(*) AS n FROM personality_views
                   WHERE last_accessed >= datetime('now', '-1 hour')
                   GROUP BY personality
                   ORDER BY n DESC"""
            ).fetchall()
            activity = {r["personality"]: r["n"] for r in row}
            total = sum(activity.values()) or 1
            blend = {p: (n / total) for p, n in activity.items()}

            # Read latest mid-session drift from any personality's most recent
            # fingerprint (cross-session drift is the ambient baseline)
            latest_fp = conn.execute(
                """SELECT personality, created_at, identity_vector
                   FROM session_fingerprints
                   WHERE identity_vector IS NOT NULL
                   ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()

            drift = None  # v1 — mid-session drift isn't persisted yet
            return {
                "center": {"x": 0.0, "y": 0.0, "z": 0.0},
                "size": 4.0,
                "base_color": PERSONALITY_COLORS["atlas"],
                "blend": blend,  # frontend interpolates color from this
                "pulse_seconds": 3.0,
                "last_fingerprint_at": latest_fp["created_at"] if latest_fp else None,
                "drift": drift,
                "clusters": [
                    {
                        "cluster_id": int(c["cluster_id"]),
                        "x": float(c["x"]),
                        "y": float(c["y"]),
                        "z": float(c["z"]),
                        "size": int(c["size"]),
                        "label": app.state.cluster_labels.get(int(c["cluster_id"]), []),
                    }
                    for c in cluster_centroids(unified_path)
                ],
            }
        finally:
            conn.close()

    @app.get("/nebula/fact/{fact_id}")
    def get_fact(fact_id: str):
        """Full fact detail for hover/click. Phase 5.3 — handles mistake
        ids (``m_<row>``) by returning a mistake-shaped payload instead."""
        conn = _conn(unified_path)
        try:
            if fact_id.startswith("m_"):
                try:
                    mid = int(fact_id[2:])
                except ValueError:
                    raise HTTPException(status_code=404, detail="bad mistake id")
                mrow = conn.execute(
                    "SELECT * FROM mistakes WHERE id=?", (mid,)
                ).fetchone()
                if mrow is None:
                    raise HTTPException(status_code=404, detail="mistake not found")
                return {
                    "id": fact_id,
                    "type": "mistake",
                    "fact": f"MISTAKE: {mrow['mistake']}"
                            + (f" — {mrow['why']}" if mrow["why"] else ""),
                    "why": mrow["why"],
                    "project": mrow["project"],
                    "personality": mrow["personality"],
                    "confidence": mrow["confidence"],
                    "created_at": mrow["created_at"],
                    "tier": None,
                    "anchor_type": None,
                    "anchor_note": None,
                    "cluster_label": [],
                    "related": [],
                    "personality_views": [
                        {"personality": mrow["personality"],
                         "last_accessed": mrow["created_at"],
                         "access_count": 0}
                    ] if mrow["personality"] else [],
                    "access_count": 0,
                }
            row = conn.execute(
                "SELECT * FROM facts WHERE id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="fact not found")
            views = conn.execute(
                """SELECT personality, last_accessed, access_count
                   FROM personality_views WHERE fact_id = ?""",
                (fact_id,),
            ).fetchall()

            related_ids = [
                r["related_id"] for r in conn.execute(
                    "SELECT related_id FROM fact_edges WHERE fact_id = ?",
                    (fact_id,),
                ).fetchall()
            ]
            related = []
            if related_ids:
                placeholders = ",".join("?" * len(related_ids))
                rel_rows = conn.execute(
                    f"SELECT id, fact FROM facts WHERE id IN ({placeholders})",
                    related_ids,
                ).fetchall()
                related = [{"id": r["id"], "preview": (r["fact"] or "")[:80]}
                           for r in rel_rows]

            return {
                "id": row["id"],
                "fact": row["fact"],
                "project": row["project"],
                "tier": row["tier"],
                "anchor_type": row["anchor_type"],
                "anchor_note": row["anchor_note"],
                "confidence": row["confidence"],
                "base_confidence": row["base_confidence"],
                "impact": row["impact"],
                "source": row["source"],
                "created_at": row["created_at"],
                "last_accessed": row["last_accessed"],
                "access_count": row["access_count"],
                "cluster_id": row["cluster_id"],
                "cluster_label": app.state.cluster_labels.get(
                    int(row["cluster_id"]) if row["cluster_id"] is not None else -1,
                    [],
                ),
                "personality_views": [dict(v) for v in views],
                "related": related,
            }
        finally:
            conn.close()

    # ── Websocket firing stream ────────────────────────────────────────

    @app.get("/nebula/recent-events")
    def recent_events(limit: int = 20):
        """Return the most recent typed events for initial EventLog fill.

        Phase 5.3b — before this endpoint, the EventLog panel stayed empty
        across refreshes until something new fired. Pete wants continuity —
        refreshing shouldn't feel like the app reset."""
        events, _ = _poll_typed_events(unified_path, after_id=0)
        events.sort(key=lambda e: e.get("at") or "", reverse=True)
        return {"events": events[:max(1, min(limit, 100))]}

    # ── Phase 7.3 — outreach trigger management ──────────────────────

    @app.get("/nebula/triggers")
    def get_triggers():
        """List every outreach_trigger with computed cooldown state.

        Frontend uses this to render the management panel."""
        conn = _conn(unified_path)
        try:
            rows = conn.execute(
                """SELECT id, name, kind, payload, enabled, cooldown_hours,
                          urgency, last_fired_at, last_fired_detail
                   FROM outreach_triggers
                   ORDER BY enabled DESC, name ASC"""
            ).fetchall()
            now = datetime.now(timezone.utc)
            triggers = []
            for r in rows:
                t = dict(r)
                # parse payload defensively
                try:
                    t["payload"] = json.loads(t.get("payload") or "{}")
                except (json.JSONDecodeError, TypeError):
                    t["payload"] = {}
                # Compute next-eligible time + state badge
                t["state"] = "ready"
                t["next_eligible_at"] = None
                if t.get("last_fired_at"):
                    try:
                        last = datetime.fromisoformat(t["last_fired_at"])
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=timezone.utc)
                        cool = float(t.get("cooldown_hours") or 0)
                        nxt = last + timedelta(hours=cool)
                        t["next_eligible_at"] = nxt.isoformat()
                        if nxt > now:
                            t["state"] = "cooling"
                    except (ValueError, TypeError):
                        pass
                if not t["enabled"]:
                    t["state"] = "disabled"
                triggers.append(t)
            return {"triggers": triggers}
        finally:
            conn.close()

    @app.patch("/nebula/triggers/{trigger_id}")
    def patch_trigger(trigger_id: int, body: dict = Body(...)):
        """Toggle enabled (and possibly other fields later).
        Body: {"enabled": 0|1}."""
        conn = _conn(unified_path)
        try:
            if "enabled" in body:
                conn.execute(
                    "UPDATE outreach_triggers SET enabled=? WHERE id=?",
                    (int(bool(body["enabled"])), trigger_id),
                )
                conn.commit()
            row = conn.execute(
                "SELECT id, name, enabled FROM outreach_triggers WHERE id=?",
                (trigger_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="trigger not found")
            return dict(row)
        finally:
            conn.close()

    @app.get("/nebula/outreaches")
    def get_outreaches(limit: int = 20, since: str | None = None):
        """Recent outreaches with acknowledgment state. Drives the
        'Recent outreaches' panel + the Phase 7.2 acknowledge flow."""
        conn = _conn(unified_path)
        try:
            params: list = []
            where = ""
            if since:
                where = "WHERE o.sent_at > ?"
                params.append(since)
            params.append(max(1, min(limit, 200)))
            rows = conn.execute(
                f"""SELECT o.id, o.trigger_id, o.personality, o.channel,
                          o.subject, o.body, o.urgency, o.delivered,
                          o.sent_at, o.acknowledged_at,
                          t.name AS trigger_name, t.kind AS trigger_kind
                   FROM outreaches o
                   LEFT JOIN outreach_triggers t ON t.id = o.trigger_id
                   {where}
                   ORDER BY o.sent_at DESC LIMIT ?""",
                params,
            ).fetchall()
            return {"outreaches": [dict(r) for r in rows]}
        finally:
            conn.close()

    @app.post("/nebula/outreaches/{outreach_id}/acknowledge")
    def acknowledge_outreach(outreach_id: int):
        """Phase 7.2 v1 — mark an outreach as acknowledged. Briefing
        will then exclude it from the 'unacknowledged since last
        session' surface."""
        conn = _conn(unified_path)
        try:
            cur = conn.execute(
                "UPDATE outreaches SET acknowledged_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), outreach_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="outreach not found")
            return {"ok": True, "id": outreach_id}
        finally:
            conn.close()

    @app.websocket("/nebula/events")
    async def events_ws(ws: WebSocket):
        """Stream typed firing events to connected frontends.

        Reads from nebula_events (written by agent.py on every observe /
        recall / learn / decide / mistake / anchor). Emits JSON records
        with kind, fact_id, personality, related_ids, intensity.
        """
        # HTTP middleware doesn't cover websockets — enforce the token
        # here (query param is the browser-friendly form; header also ok).
        if not _token_ok(ws.headers.get("authorization"),
                         ws.query_params.get("token")):
            await ws.close(code=1008, reason="missing or invalid token")
            return
        await ws.accept()
        last_id = _last_event_id(unified_path)
        try:
            while True:
                events, last_id = _poll_typed_events(unified_path, after_id=last_id)
                if events:
                    await ws.send_json({"events": events})
                await asyncio.sleep(EVENT_POLL_SECONDS)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            pass

    # ── Static frontend ───────────────────────────────────────────────

    # If the frontend was built (npm run build), serve dist/ at /
    dist_dir = Path(__file__).resolve().parent.parent.parent.parent / "nebula-web" / "dist"
    if dist_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(dist_dir), html=True),
                  name="nebula-web")

        @app.get("/")
        def index():
            return FileResponse(str(dist_dir / "index.html"))

    return app


def _last_event_id(unified_path: str) -> int:
    conn = _conn(unified_path)
    try:
        row = conn.execute("SELECT MAX(id) FROM nebula_events").fetchone()
        return (row[0] or 0) if row else 0
    finally:
        conn.close()


def _poll_typed_events(
    unified_path: str, after_id: int
) -> tuple[list[dict], int]:
    """Read typed firing events from nebula_events (id > after_id).

    Returns (events, new_last_id). Empty list if no new events.
    """
    conn = _conn(unified_path)
    try:
        # LEFT JOIN facts so we can emit position + metadata with the event.
        # Frontend uses this to append new points on-the-fly (Session 3a)
        # without needing to re-fetch the full snapshot. Phase 5.3 also
        # joins mistakes — a mistake event's fact_id is 'm_<row>', so we
        # extract the numeric tail and match against mistakes.id.
        rows = conn.execute(
            """SELECT e.id, e.kind, e.fact_id, e.personality, e.related_ids,
                      e.intensity, e.created_at,
                      f.viz_x AS f_x, f.viz_y AS f_y, f.viz_z AS f_z,
                      f.fact, f.anchor_type, f.tier, f.cluster_id,
                      m.viz_x AS m_x, m.viz_y AS m_y, m.viz_z AS m_z,
                      m.mistake
               FROM nebula_events e
               LEFT JOIN facts f ON f.id = e.fact_id
               LEFT JOIN mistakes m
                 ON (substr(e.fact_id, 1, 2) = 'm_'
                     AND m.id = CAST(substr(e.fact_id, 3) AS INTEGER))
               WHERE e.id > ?
               ORDER BY e.id ASC
               LIMIT 100""",
            (after_id,),
        ).fetchall()
        if not rows:
            return [], after_id
        events = []
        new_last = after_id
        for r in rows:
            new_last = max(new_last, r["id"])
            try:
                related = json.loads(r["related_ids"] or "[]")
            except json.JSONDecodeError:
                related = []
            event: dict = {
                "kind": r["kind"],
                "fact_id": r["fact_id"],
                "personality": r["personality"],
                "related_ids": related,
                "intensity": r["intensity"],
                "at": r["created_at"],
            }
            # Include position + metadata so frontend can render a ring at
            # the right spot and append a brand-new point if needed.
            if r["f_x"] is not None:
                event["point"] = {
                    "id": r["fact_id"],
                    "x": r["f_x"],
                    "y": r["f_y"],
                    "z": r["f_z"],
                    "fact_preview": (r["fact"] or "")[:120],
                    "anchor_type": r["anchor_type"],
                    "tier": r["tier"],
                    "cluster_id": r["cluster_id"],
                    "type": "fact",
                }
            elif r["m_x"] is not None:
                event["point"] = {
                    "id": r["fact_id"],
                    "x": r["m_x"],
                    "y": r["m_y"],
                    "z": r["m_z"],
                    "fact_preview": f"MISTAKE: {(r['mistake'] or '')[:100]}",
                    "anchor_type": None,
                    "tier": None,
                    "cluster_id": None,
                    "type": "mistake",
                }
            events.append(event)
        return events, new_last
    finally:
        conn.close()
