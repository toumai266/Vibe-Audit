import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from api import main_router, sse_router
from db import init_db
from mcp.server import get_mcp_server

# Load environment variables
load_dotenv()

# CORS middleware for frontend
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:5173",
    "http://127.0.0.1:3001",
]


async def _session_reaper():
    """Background task: auto-close sessions with no events for 5 minutes."""
    from db.database import SessionLocal
    from db.models import Session as SessionModel, Event, Flag
    from sqlalchemy import func
    while True:
        db = None
        try:
            await asyncio.sleep(60)
            db = SessionLocal()
            # Find open sessions (no end_ts)
            open_sessions = db.query(SessionModel).filter(SessionModel.end_ts == None).all()
            now = datetime.utcnow()
            for s in open_sessions:
                latest_event = db.query(func.max(Event.ts)).filter(Event.session_id == s.id).scalar()
                last_activity = latest_event or s.start_ts
                if (now - last_activity).total_seconds() > 300:  # 5 minutes
                    s.end_ts = now
                    # Set final verdict
                    high = db.query(Flag).filter(Flag.session_id == s.id, Flag.severity == "high").count()
                    if high > 0:
                        s.verdict = "off_track"
                    elif db.query(Flag).filter(Flag.session_id == s.id).count() > 0:
                        s.verdict = "needs_fix"
                    elif s.verdict == "unknown":
                        s.verdict = "ok"
                    db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Session reaper error: {e}")
        finally:
            if db is not None:
                db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize resources and manage long-lived background tasks."""
    init_db()
    # Initialize MCP server with DB session factory
    from db.database import SessionLocal
    get_mcp_server(SessionLocal)
    # Start background session reaper
    reaper_task = asyncio.create_task(_session_reaper())
    print("Vibe Audit daemon started")
    print("Database initialized")
    print("MCP Server ready at /mcp")
    try:
        yield
    finally:
        reaper_task.cancel()
        with suppress(asyncio.CancelledError):
            await reaper_task
        print("Vibe Audit daemon shutting down")


# Create FastAPI app
app = FastAPI(
    title="Vibe Audit",
    description="Agent Session Auditor - Intent Drift Monitor",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def disable_html_cache(request: Request, call_next):
    """
    Prevent stale UI shell (index.html) from being cached.
    Hashed asset files remain cache-friendly.
    """
    response = await call_next(request)
    path = request.url.path or ""
    if path in ("/", "/index.html"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response

# Include routers
app.include_router(main_router)
app.include_router(sse_router)

# MCP Endpoint
@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """MCP JSON-RPC endpoint for CLI agents"""
    try:
        body = await request.json()
        mcp_server = get_mcp_server()
        response = mcp_server.handle_request(body)
        return JSONResponse(content=response)
    except Exception as e:
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {str(e)}"}
            },
            status_code=400
        )


@app.get("/mcp/tools")
async def mcp_tools():
    """List available MCP tools (for debugging)"""
    mcp_server = get_mcp_server()
    return {"tools": mcp_server.get_tool_list()}


# Serve built React frontend (static files) — must be AFTER all API routes
static_dir = os.getenv("VIBE_AUDIT_STATIC", "")
if not static_dir:
    # Check for static/ next to daemon/ (dev layout)
    _candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")
    if os.path.isdir(_candidate):
        static_dir = _candidate

if static_dir and os.path.isdir(static_dir):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

