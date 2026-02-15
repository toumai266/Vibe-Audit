"""
Vibe Audit MCP Server
Provides tools for CLI agents (Gemini, Claude) to report session events.

MCP (Model Context Protocol) allows AI assistants to use external tools.
This server exposes tools that let CLI agents log their activities.
"""

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, asdict

# Tool definitions that CLI agents can call
MCP_TOOLS = [
    {
        "name": "vibe_session_start",
        "description": "Start a new Vibe Audit session. Call this when beginning a coding task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The agent identifier (gemini, claude, etc.)"
                },
                "project_path": {
                    "type": "string",
                    "description": "The working directory path"
                },
                "initial_goal": {
                    "type": "string",
                    "description": "The user's initial request or goal"
                }
            },
            "required": ["agent_id", "project_path"]
        }
    },
    {
        "name": "vibe_session_end",
        "description": "End the current Vibe Audit session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session ID to end"
                },
                "status": {
                    "type": "string",
                    "enum": ["completed", "cancelled", "error"],
                    "description": "Final session status"
                }
            },
            "required": ["session_id"]
        }
    },
    {
        "name": "vibe_log_event",
        "description": "Log an event to the current session (task start, tool use, response, etc.)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session ID"
                },
                "event_type": {
                    "type": "string",
                    "enum": ["task_start", "task_complete", "tool_use", "tool_result", "user_message", "assistant_response"],
                    "description": "Type of event"
                },
                "payload": {
                    "type": "object",
                    "description": "Event-specific data"
                }
            },
            "required": ["session_id", "event_type"]
        }
    },
    {
        "name": "vibe_log_snapshot",
        "description": "Log a context snapshot (current goal, plan, tasks)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session ID"
                },
                "current_goal": {
                    "type": "string",
                    "description": "Current goal being worked on"
                },
                "current_plan": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Current plan steps"
                },
                "current_tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "status": {"type": "string"}
                        }
                    },
                    "description": "Current task list"
                },
                "alignment": {
                    "type": "string",
                    "enum": ["aligned", "drifting", "unknown"],
                    "description": "Whether actions align with original goal"
                }
            },
            "required": ["session_id"]
        }
    }
]


class MCPServer:
    """
    Simple MCP Server that handles JSON-RPC requests from CLI agents.
    Can be used via stdio or HTTP endpoint.
    """
    
    def __init__(self, db_session_factory=None):
        self.db_session_factory = db_session_factory
        self.active_sessions: Dict[str, dict] = {}

    @staticmethod
    def _normalize_alignment(alignment: str) -> str:
        """Normalize legacy alignment values."""
        if alignment == "drifted":
            return "drifting"
        return alignment or "unknown"
    
    def get_tool_list(self) -> List[dict]:
        """Return list of available tools"""
        return MCP_TOOLS
    
    def handle_request(self, request: dict) -> dict:
        """Handle incoming JSON-RPC request"""
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")
        
        try:
            if method == "initialize":
                return self._handle_initialize(req_id, params)
            elif method == "tools/list":
                return self._handle_tools_list(req_id)
            elif method == "tools/call":
                return self._handle_tools_call(req_id, params)
            else:
                return self._error_response(req_id, -32601, f"Method not found: {method}")
        except Exception as e:
            return self._error_response(req_id, -32603, str(e))
    
    def _handle_initialize(self, req_id: Any, params: dict) -> dict:
        """Handle MCP initialize request"""
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "vibe-audit",
                    "version": "1.0.0"
                }
            }
        }
    
    def _handle_tools_list(self, req_id: Any) -> dict:
        """Return list of available tools"""
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": MCP_TOOLS
            }
        }
    
    def _handle_tools_call(self, req_id: Any, params: dict) -> dict:
        """Handle tool call"""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        
        # Route to appropriate handler
        handlers = {
            "vibe_session_start": self._tool_session_start,
            "vibe_session_end": self._tool_session_end,
            "vibe_log_event": self._tool_log_event,
            "vibe_log_snapshot": self._tool_log_snapshot,
        }
        
        handler = handlers.get(tool_name)
        if not handler:
            return self._error_response(req_id, -32602, f"Unknown tool: {tool_name}")
        
        result = handler(arguments)
        
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result)
                    }
                ]
            }
        }
    
    def _tool_session_start(self, args: dict) -> dict:
        """Start a new session"""
        session_id = str(uuid.uuid4())
        
        session_data = {
            "id": session_id,
            "agent_id": args.get("agent_id", "unknown"),
            "project_path": args.get("project_path", ""),
            "initial_goal": args.get("initial_goal", ""),
            "start_ts": datetime.utcnow().isoformat(),
            "events": [],
            "snapshots": []
        }
        
        self.active_sessions[session_id] = session_data
        
        # Persist to DB if available
        if self.db_session_factory:
            self._persist_session_start(session_data)
        
        return {
            "success": True,
            "session_id": session_id,
            "message": f"Session started: {session_id[:8]}..."
        }
    
    def _tool_session_end(self, args: dict) -> dict:
        """End a session"""
        session_id = args.get("session_id", "")
        status = args.get("status", "completed")
        
        if session_id in self.active_sessions:
            session = self.active_sessions.pop(session_id)
            session["end_ts"] = datetime.utcnow().isoformat()
            session["status"] = status
            
            # Persist to DB if available
            if self.db_session_factory:
                self._persist_session_end(session_id, status)
            
            return {
                "success": True,
                "message": f"Session ended: {session_id[:8]}..."
            }
        
        return {
            "success": False,
            "message": f"Session not found: {session_id}"
        }
    
    def _tool_log_event(self, args: dict) -> dict:
        """Log an event"""
        session_id = args.get("session_id", "")
        event_type = args.get("event_type", "unknown")
        payload = args.get("payload", {})
        
        event = {
            "id": str(uuid.uuid4()),
            "type": event_type,
            "ts": datetime.utcnow().isoformat(),
            "payload": payload
        }
        
        if session_id in self.active_sessions:
            self.active_sessions[session_id]["events"].append(event)
        
        # Persist to DB if available
        if self.db_session_factory:
            self._persist_event(session_id, event)
        
        return {
            "success": True,
            "event_id": event["id"]
        }
    
    def _tool_log_snapshot(self, args: dict) -> dict:
        """Log a context snapshot"""
        session_id = args.get("session_id", "")
        alignment = self._normalize_alignment(args.get("alignment", "unknown"))
        
        snapshot = {
            "id": str(uuid.uuid4()),
            "ts": datetime.utcnow().isoformat(),
            "current_goal": args.get("current_goal"),
            "current_plan": args.get("current_plan", []),
            "current_tasks": args.get("current_tasks", []),
            "alignment": alignment
        }
        
        if session_id in self.active_sessions:
            self.active_sessions[session_id]["snapshots"].append(snapshot)
        
        # Persist to DB if available
        if self.db_session_factory:
            self._persist_snapshot(session_id, snapshot)
        
        return {
            "success": True,
            "snapshot_id": snapshot["id"]
        }
    
    def _persist_session_start(self, session_data: dict):
        """Persist session start to database and trigger baseline generation"""
        try:
            from db.models import Session, Event as EventModel
            db = self.db_session_factory()
            # Session model uses cli_command field to store agent info
            cli_info = f"{session_data['agent_id']}:{session_data.get('project_path', '')}"
            session = Session(
                id=session_data["id"],
                cli_command=cli_info,
                start_ts=datetime.fromisoformat(session_data["start_ts"])
            )
            db.add(session)
            db.commit()

            # If initial_goal provided, create a session_start event to trigger baseline
            initial_goal = session_data.get("initial_goal", "")
            if initial_goal:
                start_event = EventModel(
                    session_id=session_data["id"],
                    type="session_start",
                    ts=datetime.fromisoformat(session_data["start_ts"]),
                    payload={"user_prompt": initial_goal, "cli_command": cli_info}
                )
                db.add(start_event)
                db.commit()
                db.refresh(start_event)
                self._trigger_processor(db, session_data["id"], start_event)
        except Exception as e:
            print(f"DB error: {e}")
    
    def _persist_session_end(self, session_id: str, status: str):
        """Persist session end to database"""
        try:
            from db.models import Session
            db = self.db_session_factory()
            session = db.query(Session).filter(Session.id == session_id).first()
            if session:
                session.end_ts = datetime.utcnow()
                # Note: Session model uses verdict instead of status
                db.commit()
            db.close()
        except Exception as e:
            print(f"DB error: {e}")
    
    def _persist_event(self, session_id: str, event: dict):
        """Persist event to database and trigger processor pipeline"""
        try:
            from db.models import Event as EventModel
            db = self.db_session_factory()
            db_event = EventModel(
                id=event["id"],
                session_id=session_id,
                type=event["type"],
                ts=datetime.fromisoformat(event["ts"]),
                payload=event["payload"]
            )
            db.add(db_event)
            db.commit()
            db.refresh(db_event)

            # Trigger processor pipeline asynchronously
            self._trigger_processor(db, session_id, db_event)
        except Exception as e:
            print(f"DB error: {e}")

    def _trigger_processor(self, db, session_id: str, db_event):
        """Trigger the analysis processor for an event."""
        try:
            import asyncio
            from core.processor import process_event

            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(process_event(db, session_id, db_event))
            else:
                loop.run_until_complete(process_event(db, session_id, db_event))
        except RuntimeError:
            # No event loop - create one
            import asyncio
            from core.processor import process_event
            asyncio.run(process_event(db, session_id, db_event))
        except Exception as e:
            print(f"Processor trigger failed: {e}")
    
    def _persist_snapshot(self, session_id: str, snapshot: dict):
        """Persist snapshot to database"""
        try:
            from db.models import Snapshot, Event
            db = self.db_session_factory()
            
            # Get the latest event for this session to link the snapshot
            latest_event = db.query(Event).filter(
                Event.session_id == session_id
            ).order_by(Event.ts.desc()).first()
            
            event_id = latest_event.id if latest_event else None
            
            # Skip if no event to link to (shouldn't happen in normal use)
            if not event_id:
                print(f"Warning: No event found for session {session_id}, creating without event link")
            
            db_snapshot = Snapshot(
                id=snapshot["id"],
                session_id=session_id,
                event_id=event_id,  # Link to latest event
                ts=datetime.fromisoformat(snapshot["ts"]),
                current_goal=snapshot.get("current_goal") or "",
                current_plan=snapshot.get("current_plan", []),
                current_tasks=snapshot.get("current_tasks", []),
                alignment=snapshot.get("alignment", "unknown"),
                drift_score=snapshot.get("drift_score", 0.0)
            )
            db.add(db_snapshot)
            db.commit()
            db.close()
        except Exception as e:
            print(f"DB error persisting snapshot: {e}")
    
    def _error_response(self, req_id: Any, code: int, message: str) -> dict:
        """Return JSON-RPC error response"""
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": code,
                "message": message
            }
        }


# Global server instance
_server: Optional[MCPServer] = None

def get_mcp_server(db_session_factory=None) -> MCPServer:
    """Get or create MCP server instance"""
    global _server
    if _server is None:
        _server = MCPServer(db_session_factory)
    return _server
