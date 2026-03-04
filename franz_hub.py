"""franz_hub.py -- The motherboard of Franz-AI v2.

Start with:  python franz_hub.py
Optional:    python franz_hub.py --brain my_brain.py

Pure plumbing. Owns all queues, the HTTP server, SSE, two VLM channels
(orchestrator + agent), capture loop, action executor.

Python makes ZERO decisions. It routes strings and executes actions.
"""

import asyncio
import base64
import http.server
import importlib.util
import json
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HERE: Path = Path(__file__).resolve().parent
WIN32_PATH: Path = HERE / "win32.py"
CONFIG_PATH: Path = HERE / "config.json"
PANEL_PATH: Path = HERE / "panel.html"
BOARD_PATH: Path = HERE / "hub_board.html"

NORM: int = 1000

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_config: dict[str, Any] = {}


def _load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            return json.loads(fh.read())
    return {}


def _save_config(data: dict[str, Any]) -> None:
    global _config
    _config = data
    with CONFIG_PATH.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2, ensure_ascii=True))


def cfg(key: str, default: Any = None) -> Any:
    return _config.get(key, default)


# ---------------------------------------------------------------------------
# Coordinate helpers (public, for brain use)
# ---------------------------------------------------------------------------

def _clamp(value: int) -> int:
    return max(0, min(NORM, value))


def click(x: int, y: int) -> dict[str, Any]:
    return {"type": "click", "x": _clamp(x), "y": _clamp(y)}


def double_click(x: int, y: int) -> dict[str, Any]:
    return {"type": "double_click", "x": _clamp(x), "y": _clamp(y)}


def right_click(x: int, y: int) -> dict[str, Any]:
    return {"type": "right_click", "x": _clamp(x), "y": _clamp(y)}


def type_text(text: str) -> dict[str, Any]:
    return {"type": "type_text", "params": text}


def press_key(name: str) -> dict[str, Any]:
    return {"type": "press_key", "params": name}


def hotkey(combo: str) -> dict[str, Any]:
    return {"type": "hotkey", "params": combo}


def scroll_up(x: int, y: int) -> dict[str, Any]:
    return {"type": "scroll_up", "x": _clamp(x), "y": _clamp(y)}


def scroll_down(x: int, y: int) -> dict[str, Any]:
    return {"type": "scroll_down", "x": _clamp(x), "y": _clamp(y)}


def drag(x1: int, y1: int, x2: int, y2: int) -> dict[str, Any]:
    return {"type": "drag", "x1": _clamp(x1), "y1": _clamp(y1),
            "x2": _clamp(x2), "y2": _clamp(y2)}


# ---------------------------------------------------------------------------
# Overlay helpers (public, for brain use)
# ---------------------------------------------------------------------------

def dot(x: int, y: int, label: str = "", color: str = "#00ff00") -> dict[str, Any]:
    return {
        "points": [[x, y]], "closed": False,
        "stroke": color, "fill": "",
        "label": label, "label_position": [x, y],
        "label_style": {"font_size": 10, "bg": "", "color": color, "align": "left"},
    }


def box(
    x1: int, y1: int, x2: int, y2: int,
    label: str = "", stroke_color: str = "#ff6600", fill_color: str = "",
) -> dict[str, Any]:
    return {
        "points": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
        "closed": True,
        "stroke": stroke_color, "fill": fill_color,
        "label": label, "label_position": [x1, y1],
        "label_style": {"font_size": 10, "bg": "", "color": stroke_color, "align": "left"},
    }


def line(
    points: list[list[int]], label: str = "", color: str = "#4488ff",
) -> dict[str, Any]:
    return {
        "points": points, "closed": False,
        "stroke": color, "fill": "",
        "label": label,
        "label_position": points[0] if points else [0, 0],
        "label_style": {"font_size": 10, "bg": "", "color": color, "align": "left"},
    }


# ---------------------------------------------------------------------------
# Action routing (trivial string split -- NO regex, NO intelligence)
# ---------------------------------------------------------------------------

def route_action_string(action_str: str) -> dict[str, Any] | None:
    """Convert a strictly-formatted action string to a hub action dict.

    Expected input from PARSER agent: exactly one of
        click(x,y)  double_click(x,y)  right_click(x,y)
        type_text(text)  press_key(name)  hotkey(combo)
        scroll_up(x,y)  scroll_down(x,y)  drag(x1,y1,x2,y2)
        wait()  done()

    This function does TRIVIAL string splitting only.
    The PARSER agent is responsible for producing clean format.
    Returns None if format is unrecognized.
    """
    s: str = action_str.strip()
    if "(" not in s or not s.endswith(")"):
        return None

    paren_idx: int = s.index("(")
    cmd: str = s[:paren_idx].strip().lower()
    args_str: str = s[paren_idx + 1:-1].strip()

    if cmd == "wait":
        return {"_special": "wait"}
    if cmd == "done":
        return {"_special": "done"}
    if cmd == "abandon":
        return {"_special": "abandon"}

    args: list[str] = [a.strip() for a in args_str.split(",")]

    try:
        if cmd == "click" and len(args) >= 2:
            return click(int(args[0]), int(args[1]))
        if cmd == "double_click" and len(args) >= 2:
            return double_click(int(args[0]), int(args[1]))
        if cmd == "right_click" and len(args) >= 2:
            return right_click(int(args[0]), int(args[1]))
        if cmd == "type_text" and args_str:
            # Remove surrounding quotes if present
            txt: str = args_str
            if txt.startswith('"') and txt.endswith('"'):
                txt = txt[1:-1]
            return type_text(txt)
        if cmd == "press_key" and args:
            return press_key(args[0])
        if cmd == "hotkey" and args:
            return hotkey(args[0])
        if cmd == "scroll_up" and len(args) >= 2:
            return scroll_up(int(args[0]), int(args[1]))
        if cmd == "scroll_down" and len(args) >= 2:
            return scroll_down(int(args[0]), int(args[1]))
        if cmd == "drag" and len(args) >= 4:
            return drag(int(args[0]), int(args[1]),
                        int(args[2]), int(args[3]))
    except (ValueError, TypeError, IndexError):
        return None

    return None


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None

_frame_b64: str = ""
_frame_seq: int = 0
_frame_event: asyncio.Event | None = None
_capture_requested: asyncio.Event | None = None
_overlays_pending: list[dict[str, Any]] = []
_overlays_lock: threading.Lock = threading.Lock()

_action_queue: asyncio.Queue[dict[str, Any]] | None = None

# Two VLM channels
_vlm_orchestrator_semaphore: asyncio.Semaphore | None = None
_vlm_agent_semaphore: asyncio.Semaphore | None = None
_agent_semaphore: asyncio.Semaphore | None = None

_swarm_messages: list[dict[str, Any]] = []
_swarm_lock: threading.Lock = threading.Lock()

_event_log: list[dict[str, Any]] = []
_event_log_lock: threading.Lock = threading.Lock()

_agent_states: dict[str, dict[str, Any]] = {}
_agent_states_lock: threading.Lock = threading.Lock()

# Annotation pipeline
_ann_pending_seq: int = 0
_ann_result_b64: str = ""
_ann_ready: asyncio.Event | None = None
_raw_b64_for_panel: str = ""
_overlays_for_panel: list[dict[str, Any]] = []

# Session logging
_session_dir: Path | None = None
_log_file: Path | None = None


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _init_session() -> None:
    global _session_dir, _log_file
    if not cfg("log_to_disk", True):
        return
    log_dir: Path = HERE / cfg("log_dir", "logs")
    log_dir.mkdir(exist_ok=True)
    _session_dir = log_dir / _utc_stamp()
    _session_dir.mkdir(exist_ok=True)
    _log_file = _session_dir / "events.txt"


def _log_to_disk(text: str) -> None:
    if _log_file is None:
        return
    try:
        with _log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"[{_utc_stamp()}] {text}\n")
    except OSError:
        pass


def _save_frame_to_disk(b64: str) -> None:
    if _session_dir is None:
        return
    try:
        (_session_dir / f"{_utc_stamp()}.png").write_bytes(base64.b64decode(b64))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# SSE Event Bus
# ---------------------------------------------------------------------------

MAX_SSE_SUBSCRIBERS: int = 5


class _EventBus:
    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._subs: list[dict[str, Any]] = []

    def subscribe(self) -> dict[str, Any]:
        import queue as _q
        sub: dict[str, Any] = {"queue": _q.Queue(), "active": True}
        with self._lock:
            if len(self._subs) >= MAX_SSE_SUBSCRIBERS:
                oldest = self._subs[0]
                oldest["active"] = False
                try:
                    oldest["queue"].put_nowait(None)
                except Exception:
                    pass
                self._subs.pop(0)
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: dict[str, Any]) -> None:
        sub["active"] = False
        with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        payload: dict[str, Any] = {"event": event_type, "data": data}
        with self._lock:
            dead: list[dict[str, Any]] = []
            for sub in self._subs:
                if not sub["active"]:
                    dead.append(sub)
                    continue
                try:
                    sub["queue"].put_nowait(payload)
                except Exception:
                    dead.append(sub)
            for d in dead:
                d["active"] = False
                try:
                    self._subs.remove(d)
                except ValueError:
                    pass


_bus: _EventBus = _EventBus()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_frame() -> str:
    """Return the most recent annotated frame (base64 PNG).
    Blocks until at least one frame is available.
    """
    if _frame_b64:
        return _frame_b64
    if _frame_event is not None:
        await _frame_event.wait()
    return _frame_b64


def request_fresh_frame() -> None:
    """Ask the capture loop to take a new screenshot (non-blocking)."""
    if _capture_requested is not None:
        _capture_requested.set()


def swarm_message(
    agent: str,
    direction: str,
    text: str,
    image_b64: str = "",
    system: str = "",
) -> None:
    """Log a swarm wire message. Visible in panel."""
    msg: dict[str, Any] = {
        "agent": agent,
        "direction": direction,
        "text": text,
        "image_b64": image_b64,
        "system": system,
        "ts": time.time(),
    }
    with _swarm_lock:
        idx: int = len(_swarm_messages)
        _swarm_messages.append(msg)
    _bus.publish("swarm", {
        "agent": agent, "direction": direction, "text": text,
        "has_image": bool(image_b64), "system": system,
        "ts": msg["ts"], "idx": idx,
    })
    _log_to_disk(f"[SWARM] {agent} {direction}: {text[:200]}")


def actions(action: dict[str, Any]) -> None:
    """Enqueue an input action (click, type, etc.)."""
    if _action_queue is not None and _loop is not None:
        _loop.call_soon_threadsafe(_action_queue.put_nowait, action)


def overlays(overlay: dict[str, Any]) -> None:
    """Add an overlay shape for the next frame render."""
    with _overlays_lock:
        _overlays_pending.append(overlay)


def log_event(text: str, level: str = "info") -> None:
    """Append to the event log (visible in panel)."""
    entry: dict[str, Any] = {"text": text, "level": level, "ts": time.time()}
    with _event_log_lock:
        _event_log.append(entry)
    _bus.publish("log", entry)
    _log_to_disk(f"[{level.upper()}] {text}")


def set_agent_status(agent: str, status: str) -> None:
    """Update an agent's display status."""
    now: float = time.time()
    with _agent_states_lock:
        prev = _agent_states.get(agent)
        _agent_states[agent] = {
            "status": status,
            "since": now,
            "prev_status": prev["status"] if prev else "idle",
        }
    _bus.publish("agent_status", {"agent": agent, "status": status, "since": now})


async def call_vlm_orchestrator(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.7,
    max_tokens: int = 512,
    agent_name: str = "ORCHESTRATOR",
) -> str:
    """Call VLM on the orchestrator channel.
    Independent semaphore -- never blocked by agent calls.
    """
    if _vlm_orchestrator_semaphore is None:
        return ""
    set_agent_status(agent_name, "thinking")
    async with _vlm_orchestrator_semaphore:
        delay: float = cfg("vlm_request_delay_seconds", 0.3)
        if delay > 0:
            await asyncio.sleep(delay)
        result: str = await _do_vlm_call(messages, temperature, max_tokens)
        set_agent_status(agent_name, "idle")
        return result


async def call_vlm_agent(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.7,
    max_tokens: int = 512,
    agent_name: str = "",
) -> str:
    """Call VLM on the agent channel.
    Semaphore-gated -- agents queue and take turns.
    """
    if _vlm_agent_semaphore is None:
        return ""
    if agent_name:
        set_agent_status(agent_name, "awaiting_vlm")
    async with _vlm_agent_semaphore:
        if agent_name:
            set_agent_status(agent_name, "thinking")
        delay: float = cfg("vlm_request_delay_seconds", 0.3)
        if delay > 0:
            await asyncio.sleep(delay)
        result: str = await _do_vlm_call(messages, temperature, max_tokens)
        if agent_name:
            set_agent_status(agent_name, "idle")
        return result


async def _do_vlm_call(
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
) -> str:
    """Shared VLM HTTP call logic."""
    endpoint: str = cfg("vlm_endpoint_url", "")
    if not endpoint:
        log_event("VLM endpoint not configured", "error")
        return ""
    body: bytes = json.dumps({
        "model": cfg("vlm_model_name", ""),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "messages": messages,
    }, ensure_ascii=True).encode("utf-8")
    timeout: int = cfg("vlm_timeout_seconds", 360)
    try:
        result: str = await asyncio.get_event_loop().run_in_executor(
            None, _vlm_http_post, endpoint, body, timeout,
        )
        return result
    except Exception as exc:
        log_event(f"VLM error: {exc}", "error")
        return ""


def _vlm_http_post(endpoint: str, body: bytes, timeout: int) -> str:
    """Synchronous VLM HTTP call (runs in executor thread)."""
    req: urllib.request.Request = urllib.request.Request(
        endpoint, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp_obj: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
    choices: list[Any] = resp_obj.get("choices", [])
    if choices and isinstance(choices[0], dict):
        msg: Any = choices[0].get("message", {})
        if isinstance(msg, dict):
            content: Any = msg.get("content", "")
            if isinstance(content, str):
                return content
    error_info: Any = resp_obj.get("error", {})
    if error_info:
        raise RuntimeError(f"VLM server error: {error_info}")
    raise RuntimeError(f"VLM unexpected response: {str(resp_obj)[:300]}")


def get_agent_semaphore() -> asyncio.Semaphore | None:
    """Return the agent-task concurrency semaphore."""
    return _agent_semaphore


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _subprocess_capture() -> str:
    cmd: list[str] = [sys.executable, str(WIN32_PATH), "capture"]
    region: str = cfg("capture_region", "")
    if region:
        cmd.extend(["--region", region])
    cmd.extend(["--width", str(cfg("capture_width", 640))])
    cmd.extend(["--height", str(cfg("capture_height", 640))])
    proc: subprocess.CompletedProcess[bytes] = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return ""
    return base64.b64encode(proc.stdout).decode("ascii")


def _subprocess_cursor_pos() -> tuple[int, int]:
    cmd: list[str] = [sys.executable, str(WIN32_PATH), "cursor_pos"]
    region: str = cfg("capture_region", "")
    if region:
        cmd.extend(["--region", region])
    proc: subprocess.CompletedProcess[bytes] = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return 500, 500
    parts: list[str] = proc.stdout.decode("ascii").strip().split(",")
    if len(parts) != 2:
        return 500, 500
    return int(parts[0]), int(parts[1])


def _action_xy_str(action: dict[str, Any]) -> str:
    return f"{int(action.get('x', 500))},{int(action.get('y', 500))}"


def _execute_action(action: dict[str, Any]) -> None:
    action_type: str = str(action.get("type", ""))
    params_str: str = str(action.get("params", ""))
    region: str = cfg("capture_region", "")
    cmd: list[str] = [sys.executable, str(WIN32_PATH)]

    match action_type:
        case "click":
            cmd.extend(["click", "--pos", _action_xy_str(action)])
        case "double_click":
            cmd.extend(["double_click", "--pos", _action_xy_str(action)])
        case "right_click":
            cmd.extend(["right_click", "--pos", _action_xy_str(action)])
        case "type_text":
            cmd.extend(["type_text", "--text", params_str])
        case "press_key":
            cmd.extend(["press_key", "--key", params_str])
        case "hotkey":
            cmd.extend(["hotkey", "--keys", params_str])
        case "scroll_up":
            cmd.extend(["scroll_up", "--pos", _action_xy_str(action)])
        case "scroll_down":
            cmd.extend(["scroll_down", "--pos", _action_xy_str(action)])
        case "drag":
            from_pos: str = f"{action.get('x1', 500)},{action.get('y1', 500)}"
            to_pos: str = f"{action.get('x2', 500)},{action.get('y2', 500)}"
            cmd.extend(["drag", "--from_pos", from_pos, "--to_pos", to_pos])
        case _:
            log_event(f"Unknown action type: {action_type}", "error")
            return

    needs_region: bool = action_type not in ("type_text", "press_key", "hotkey")
    if region and needs_region:
        cmd.extend(["--region", region])

    result: subprocess.CompletedProcess[bytes] = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        log_event(f"Action {action_type} failed: rc={result.returncode}", "error")


# ---------------------------------------------------------------------------
# Cursor overlay
# ---------------------------------------------------------------------------

def _make_cursor_overlay(cx: int, cy: int) -> dict[str, Any]:
    arm: int = cfg("cursor_arm", 14)
    color: str = cfg("cursor_color", "#ff4444")
    show: bool = cfg("show_cursor", True)
    label_text: str = f"[{cx},{cy}]" if show else ""
    return {
        "points": [
            [cx - arm, cy], [cx + arm, cy],
            [cx, cy], [cx, cy - arm], [cx, cy + arm],
        ],
        "closed": False, "stroke": color, "fill": "",
        "label": label_text,
        "label_position": [min(cx + 18, 980), min(cy + 18, 980)],
        "label_style": {
            "font_size": 11, "bg": "#000000", "color": color, "align": "left",
        },
    }


# ---------------------------------------------------------------------------
# Async tasks: capture loop, action executor
# ---------------------------------------------------------------------------

async def _capture_loop() -> None:
    """Continuously capture screenshots and send to panel for annotation."""
    global _frame_b64, _frame_seq, _raw_b64_for_panel, _overlays_for_panel
    global _ann_pending_seq, _ann_result_b64

    interval: float = cfg("capture_interval_seconds", 2.0)

    log_event("Waiting for panel connection...")
    _bus.publish("state", _build_state_snapshot())
    while not _panel_connected.is_set():
        await asyncio.sleep(0.2)
    log_event("Panel connected", "ok")
    await asyncio.sleep(0.5)

    while True:
        try:
            await asyncio.wait_for(_capture_requested.wait(), timeout=interval)
            _capture_requested.clear()
        except asyncio.TimeoutError:
            pass

        raw: str = await asyncio.get_event_loop().run_in_executor(
            None, _subprocess_capture,
        )
        if not raw:
            log_event("Capture failed", "error")
            await asyncio.sleep(1.0)
            continue

        with _overlays_lock:
            frame_overlays: list[dict[str, Any]] = list(_overlays_pending)
            _overlays_pending.clear()

        if cfg("show_cursor", True):
            cx, cy = await asyncio.get_event_loop().run_in_executor(
                None, _subprocess_cursor_pos,
            )
            frame_overlays.append(_make_cursor_overlay(cx, cy))

        _frame_seq += 1
        _ann_pending_seq = _frame_seq
        _raw_b64_for_panel = raw
        _overlays_for_panel = frame_overlays
        _ann_result_b64 = ""
        _ann_ready.clear()

        _bus.publish("frame", {"seq": _frame_seq})
        _bus.publish("state", _build_state_snapshot())

        try:
            await asyncio.wait_for(_ann_ready.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            log_event("Annotation timeout, using raw frame", "warn")
            _ann_result_b64 = raw

        _frame_b64 = _ann_result_b64 if _ann_result_b64 else raw
        _frame_event.set()
        _frame_event.clear()

        _save_frame_to_disk(_frame_b64)
        _bus.publish("frame_done", {"seq": _frame_seq})
        _bus.publish("state", _build_state_snapshot())


async def _action_executor_loop() -> None:
    """Drain action queue and execute via win32.py subprocesses."""
    delay: float = cfg("action_delay_seconds", 0.3)
    count: int = 0

    while True:
        action: dict[str, Any] = await _action_queue.get()
        if count > 0 and delay > 0:
            await asyncio.sleep(delay)

        await asyncio.get_event_loop().run_in_executor(None, _execute_action, action)
        count += 1
        _log_to_disk(f"[ACTION] {action.get('type', '?')}")
        request_fresh_frame()


# ---------------------------------------------------------------------------
# State snapshot
# ---------------------------------------------------------------------------

_panel_connected: asyncio.Event | None = None


def _build_state_snapshot() -> dict[str, Any]:
    with _swarm_lock:
        swarm_count: int = len(_swarm_messages)
    with _agent_states_lock:
        agents: dict[str, Any] = dict(_agent_states)
    return {
        "frame_seq": _frame_seq,
        "ann_pending_seq": _ann_pending_seq,
        "swarm_count": swarm_count,
        "agents": agents,
    }


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class HubHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format_str: str, *args: object) -> None:
        pass

    def _send_json(self, code: int, data: dict[str, Any]) -> None:
        body: bytes = json.dumps(data, ensure_ascii=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def _send_file(self, code: int, content: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(content)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def _handle_sse(self) -> None:
        if _panel_connected is not None and _loop is not None:
            _loop.call_soon_threadsafe(_panel_connected.set)
        sub: dict[str, Any] = _bus.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b"event: connected\ndata: {}\n\n")
            self.wfile.flush()
            while sub["active"]:
                try:
                    payload: dict[str, Any] | None = sub["queue"].get(timeout=25)
                except Exception:
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                        break
                    continue
                if payload is None:
                    break
                evt: str = payload.get("event", "message")
                dat: str = json.dumps(payload.get("data", {}), ensure_ascii=True)
                chunk: bytes = f"event: {evt}\ndata: {dat}\n\n".encode("utf-8")
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    break
        finally:
            _bus.unsubscribe(sub)

    def do_GET(self) -> None:
        path: str = self.path.split("?", 1)[0]
        match path:
            case "/" | "/index.html":
                self._send_file(200, PANEL_PATH.read_bytes(), "text/html; charset=utf-8")
            case "/board":
                self._send_file(200, BOARD_PATH.read_bytes(), "text/html; charset=utf-8")
            case "/events":
                self._handle_sse()
            case "/state":
                if _panel_connected is not None and _loop is not None:
                    _loop.call_soon_threadsafe(_panel_connected.set)
                self._send_json(200, _build_state_snapshot())
            case "/frame":
                self._send_json(200, {
                    "seq": _ann_pending_seq,
                    "raw_b64": _raw_b64_for_panel,
                    "overlays": _overlays_for_panel,
                })
            case "/config":
                self._send_json(200, _config)
            case "/swarm":
                after: int = 0
                qs: str = self.path.split("?", 1)[1] if "?" in self.path else ""
                for param in qs.split("&"):
                    if param.startswith("after="):
                        try:
                            after = int(param[6:])
                        except ValueError:
                            pass
                with _swarm_lock:
                    msgs: list[dict[str, Any]] = _swarm_messages[after:]
                    total: int = len(_swarm_messages)
                strip: list[dict[str, Any]] = [
                    {
                        "agent": m.get("agent", ""),
                        "direction": m.get("direction", ""),
                        "text": m.get("text", ""),
                        "has_image": bool(m.get("image_b64", "")),
                        "system": m.get("system", ""),
                        "ts": m.get("ts", 0),
                    }
                    for m in msgs
                ]
                self._send_json(200, {"messages": strip, "total": total})
            case _ if path.startswith("/swarm_image/"):
                try:
                    idx: int = int(path.split("/")[2])
                except (IndexError, ValueError):
                    self._send_json(404, {"error": "bad index"})
                    return
                with _swarm_lock:
                    img_b64: str = (
                        _swarm_messages[idx].get("image_b64", "")
                        if 0 <= idx < len(_swarm_messages) else ""
                    )
                if img_b64:
                    img_bytes: bytes = base64.b64decode(img_b64)
                    self._send_file(200, img_bytes, "image/png")
                else:
                    self._send_json(404, {"error": "no image"})
            case "/event_log":
                with _event_log_lock:
                    entries: list[dict[str, Any]] = list(_event_log[-200:])
                self._send_json(200, {"entries": entries})
            case _:
                self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path: str = self.path.split("?", 1)[0]
        content_length: int = int(self.headers.get("Content-Length", "0"))
        body: bytes = self.rfile.read(content_length) if content_length > 0 else b""
        match path:
            case "/annotated":
                try:
                    parsed: Any = json.loads(body.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._send_json(400, {"ok": False, "err": "bad json"})
                    return
                if not isinstance(parsed, dict):
                    self._send_json(400, {"ok": False, "err": "bad json"})
                    return
                seq_val: Any = parsed.get("seq")
                img_val: Any = parsed.get("image_b64", "")
                if seq_val != _ann_pending_seq:
                    self._send_json(409, {
                        "ok": False,
                        "err": f"seq mismatch got={seq_val} want={_ann_pending_seq}",
                    })
                    return
                if not isinstance(img_val, str) or len(img_val) < 100:
                    self._send_json(400, {"ok": False, "err": "image too short"})
                    return
                global _ann_result_b64
                _ann_result_b64 = img_val
                if _ann_ready is not None and _loop is not None:
                    _loop.call_soon_threadsafe(_ann_ready.set)
                self._send_json(200, {"ok": True, "seq": seq_val})
            case "/config":
                try:
                    new_cfg: Any = json.loads(body.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._send_json(400, {"ok": False, "err": "bad json"})
                    return
                if not isinstance(new_cfg, dict):
                    self._send_json(400, {"ok": False, "err": "expected object"})
                    return
                _save_config(new_cfg)
                log_event("Config updated via board")
                self._send_json(200, {"ok": True})
            case _:
                self._send_json(404, {"error": "not found"})

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()


# ---------------------------------------------------------------------------
# Region selection
# ---------------------------------------------------------------------------

def _run_select_region() -> tuple[str, int]:
    proc: subprocess.CompletedProcess[bytes] = subprocess.run(
        [sys.executable, str(WIN32_PATH), "select_region"],
        capture_output=True,
    )
    if proc.returncode == 2:
        return "", 2
    if proc.returncode != 0 or not proc.stdout:
        return "", proc.returncode
    return proc.stdout.decode("ascii").strip(), 0


# ---------------------------------------------------------------------------
# Brain loader
# ---------------------------------------------------------------------------

def _load_brain(filename: str) -> Any:
    filepath: Path = HERE / filename
    if not filepath.exists():
        print(f"ERROR: {filename} not found in {HERE}")
        raise SystemExit(1)
    spec = importlib.util.spec_from_file_location("brain", str(filepath))
    if spec is None or spec.loader is None:
        print(f"ERROR: cannot load {filename}")
        raise SystemExit(1)
    module = importlib.util.module_from_spec(spec)
    sys.modules["brain"] = module
    spec.loader.exec_module(module)
    return module


def _pick_brain() -> str:
    configured: str = cfg("brain_file", "brain.py")
    path: Path = HERE / configured
    if path.exists():
        return configured
    candidates: list[Path] = sorted(HERE.glob("brain*.py"))
    if not candidates:
        print(f"ERROR: No brain*.py files found in {HERE}")
        raise SystemExit(1)
    if len(candidates) == 1:
        return candidates[0].name
    print("\nAvailable brains:")
    for idx, fp in enumerate(candidates):
        print(f"  [{idx + 1}] {fp.name}")
    while True:
        choice: str = input(f"\nSelect brain [1-{len(candidates)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            return candidates[int(choice) - 1].name
        print("Invalid choice.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _async_main(brain_module: Any) -> None:
    global _frame_event, _capture_requested, _action_queue
    global _vlm_orchestrator_semaphore, _vlm_agent_semaphore
    global _agent_semaphore, _ann_ready, _panel_connected

    _frame_event = asyncio.Event()
    _capture_requested = asyncio.Event()
    _action_queue = asyncio.Queue()
    _ann_ready = asyncio.Event()
    _panel_connected = asyncio.Event()

    _vlm_orchestrator_semaphore = asyncio.Semaphore(
        cfg("max_orchestrator_vlm_concurrent", 1),
    )
    _vlm_agent_semaphore = asyncio.Semaphore(
        cfg("max_agent_vlm_concurrent", 1),
    )
    _agent_semaphore = asyncio.Semaphore(
        cfg("max_parallel_agents", 2),
    )

    asyncio.create_task(_capture_loop())
    asyncio.create_task(_action_executor_loop())

    log_event("Hub ready, starting brain")

    brain_main = getattr(brain_module, "main", None)
    if brain_main is None:
        log_event("ERROR: brain has no async def main(hub)", "error")
        return

    hub_module = sys.modules[__name__]

    try:
        await brain_main(hub_module)
    except Exception as exc:
        log_event(f"Brain crashed: {exc}", "error")
        import traceback
        traceback.print_exc()


def main() -> None:
    global _config, _loop

    _config = _load_config()

    args: list[str] = sys.argv[1:]
    brain_override: str = ""
    skip_region: bool = False
    idx: int = 0
    while idx < len(args):
        if args[idx] == "--brain" and idx + 1 < len(args):
            brain_override = args[idx + 1]
            idx += 2
        elif args[idx] == "--skip-region":
            skip_region = True
            idx += 1
        else:
            idx += 1

    if brain_override:
        _config["brain_file"] = brain_override

    brain_filename: str = _pick_brain()
    print(f"Brain: {brain_filename}")

    if not skip_region:
        print("Select capture region (drag), right-click for full screen, Escape to quit.")
        region_str, exit_code = _run_select_region()
        if exit_code == 2:
            print("Cancelled.")
            raise SystemExit(0)
        if region_str:
            print(f"Region: {region_str}")
            _config["capture_region"] = region_str
        else:
            print("Full screen mode.")
            _config["capture_region"] = ""

    sys.modules["franz_hub"] = sys.modules[__name__]

    brain_module: Any = _load_brain(brain_filename)

    _init_session()

    host: str = cfg("server_host", "127.0.0.1")
    port: int = int(cfg("server_port", 1234))

    print(f"\nFranz Hub starting on http://{host}:{port}")
    print(f"VLM: {cfg('vlm_endpoint_url', '?')}")
    print(f"Region: {cfg('capture_region', '') or 'full screen'}")
    if _session_dir:
        print(f"Session: {_session_dir}")
    print(f"\nOpen http://{host}:{port} in Chrome to start.\n")

    server: http.server.ThreadingHTTPServer = http.server.ThreadingHTTPServer(
        (host, port), HubHandler,
    )
    http_thread: threading.Thread = threading.Thread(
        target=server.serve_forever, daemon=True,
    )
    http_thread.start()
    print(f"HTTP server running at http://{host}:{port}")

    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    try:
        _loop.run_until_complete(_async_main(brain_module))
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.shutdown()
        _loop.close()
        print("Franz Hub stopped.")


if __name__ == "__main__":
    main()
