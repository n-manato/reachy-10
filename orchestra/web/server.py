"""HTTP: four commands in, one state stream out.

aiohttp rather than a framework, because this is already a dependency of the
robot SDK and the whole surface is six routes. Nothing here knows how a
performance works -- it validates what arrived, hands it to `ShowRunner`, and
returns the same state snapshot every route returns, so the page never has to
guess what a command did.

**Binds to loopback by default, and that is a safety decision, not a
convenience.** There is no authentication here and there is no sensible way to
add one that would be honest: anything that can reach this port can start nine
robots moving. `--host 0.0.0.0` exists because an exhibition sometimes needs a
second screen, and it prints a warning every time.

State goes out as Server-Sent Events: one way, text, reconnects by itself, and
no protocol of our own to get wrong. A browser that stops reading is dropped
from the fan-out rather than allowed to back up into the runner -- a stalled
tab must not be able to slow down a show.
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections import deque
import json
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from .export import stage_bytes

STATIC = Path(__file__).parent / "static"

TICK_S = 1.0
"""How often a snapshot goes out when nothing has changed.

The page runs its own clock between ticks -- it has to, since it draws 60
frames for every one of these -- so this is a correction rate, not a frame
rate. One second keeps the drift below what anyone can see while leaving the
link essentially idle.
"""

QUEUE_DEPTH = 8
"""Snapshots held for one browser before the oldest are dropped.

A snapshot is a complete description of the present, so dropping old ones
loses nothing. The alternative -- waiting for a tab that has stopped reading --
would put a browser in a position to delay a performance.
"""


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(
        d, separators=(",", ":"), allow_nan=False))


async def _body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="expected a JSON body")
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(
        STATIC / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


async def api_state(request: web.Request) -> web.Response:
    return _json(request.app[RUNNER].snapshot())


async def api_stage(request: web.Request) -> web.Response:
    """The whole show's motion, gzipped once at startup and kept.

    Compressed in `build_app` rather than on first request: it never changes
    while the server is up (rebuilding the song means restarting this process),
    and writing it into the app afterwards mutates a running application, which
    aiohttp deprecates and will eventually refuse.
    """
    return web.Response(
        body=request.app[STAGE_BLOB],
        content_type="application/json",
        headers={
            "Content-Encoding": "gzip",
            "Cache-Control": "no-cache",
        },
    )


async def api_play(request: web.Request) -> web.Response:
    return _json(request.app[RUNNER].play())


async def api_stop(request: web.Request) -> web.Response:
    return _json(request.app[RUNNER].stop())


async def api_estop(request: web.Request) -> web.Response:
    return _json(request.app[RUNNER].estop())


async def api_loop(request: web.Request) -> web.Response:
    data = await _body(request)
    runner = request.app[RUNNER]
    on = data.get("on")
    if on is None:
        on = not runner.snapshot()["loop"]
    return _json(runner.set_loop(bool(on)))


async def api_volume(request: web.Request) -> web.Response:
    data = await _body(request)
    try:
        value = int(data["value"])
    except (KeyError, TypeError, ValueError):
        raise web.HTTPBadRequest(text='expected {"value": 0..100}')
    # Clamping rather than rejecting: the slider is the one control a person
    # drags, and refusing the ends of its own travel would be absurd.
    return _json(request.app[RUNNER].set_volume(max(0, min(100, value))))


CLIENT_LOG_MAX_BYTES = 2048
CLIENT_LOG_PER_MINUTE = 30


async def api_client_log(request: web.Request) -> web.Response:
    """Why a page did not start, from a screen nobody here can open.

    A failed boot used to leave "Loading…" up, with the reason only in the
    developer console of whatever browser it was. The page sends it here and
    it lands in this service's journal. The URL is public and unauthenticated,
    so this is bounded, rate-limited, one line per report, and echoes nothing.
    """
    recent = request.app[CLIENT_LOG]
    now = time.monotonic()
    while recent and now - recent[0] > 60.0:
        recent.popleft()
    raw = await request.content.read(CLIENT_LOG_MAX_BYTES)
    if len(recent) >= CLIENT_LOG_PER_MINUTE:
        return web.Response(status=429)
    recent.append(now)

    text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        data = {"detail": text}

    def line(key: str, n: int) -> str:
        return " ".join(str(data.get(key) or "").split())[:n]

    who = request.headers.get("CF-Connecting-IP") or request.remote
    print(f"  page {line('kind', 20) or 'report'} while {line('step', 40) or '?'}: "
          f"{line('detail', 900)} [{line('ua', 160)}] from {who}", flush=True)
    return web.Response(status=204)


async def api_events(request: web.Request) -> web.StreamResponse:
    runner = request.app[RUNNER]
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        # Nothing between here and the browser on loopback, but a proxy in
        # front of an exhibition machine would otherwise buffer the stream
        # into uselessness.
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_DEPTH)

    def offer(snap: dict) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(snap)

    def on_snapshot(snap: dict) -> None:
        # Called on the runner's thread. Everything touching the queue has to
        # happen on the event loop's.
        loop.call_soon_threadsafe(offer, snap)

    unsubscribe = runner.subscribe(on_snapshot)
    try:
        await _send(resp, runner.snapshot())
        while True:
            try:
                snap = await asyncio.wait_for(queue.get(), timeout=TICK_S)
            except asyncio.TimeoutError:
                snap = runner.snapshot()
            await _send(resp, snap)
    except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
        pass
    finally:
        unsubscribe()
    return resp


async def _send(resp: web.StreamResponse, snap: dict) -> None:
    payload = json.dumps(snap, separators=(",", ":"), allow_nan=False)
    await resp.write(f"data: {payload}\n\n".encode("utf-8"))


async def api_preview_audio(request: web.Request) -> web.StreamResponse:
    """The PC-side mix -- in rehearsal only, and never during a performance.

    **The show comes out of the robots' own speakers.** Nine stems, each played
    by the robot playing that part, on that robot's own clock. The PC is not in
    the audio path at all, and neither is this browser: the page is a control
    surface and a picture.

    This file is the render `song build` made for checking the screen when the
    fleet is switched off. Serving it during a performance would put a tenth
    voice in the room, a PC sound card's latency away from the nine that are
    supposed to be together -- so the mode is checked here rather than trusted
    to the page. A browser that cannot fetch it cannot play it, whatever its
    buttons are doing.
    """
    runner = request.app[RUNNER]
    if getattr(runner.backend, "mode", None) != "rehearsal":
        raise web.HTTPForbidden(
            text="the performance is played on the robots' own speakers. "
                 "The PC mix is served in --rehearse only.")
    path = request.app[PREVIEW_AUDIO]
    if path is None or not path.exists():
        raise web.HTTPNotFound(text="this song has no rendered preview")
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


RUNNER: web.AppKey = web.AppKey("runner")
STAGE: web.AppKey = web.AppKey("stage", dict)
STAGE_BLOB: web.AppKey = web.AppKey("stage_blob", bytes)
PREVIEW_AUDIO: web.AppKey = web.AppKey("preview_audio")
STARTED_AT: web.AppKey = web.AppKey("started_at", float)
CLIENT_LOG: web.AppKey = web.AppKey("client_log", deque)


@web.middleware
async def _no_stale_static(request: web.Request, handler):
    """Every response under /static/ revalidates.

    aiohttp's static handler sends Last-Modified and nothing else, and a
    browser given nothing else guesses a freshness of a tenth of the file's
    age -- minutes, for a file saved an hour ago. On a page that is being
    changed while someone watches it, that is a stale `environment.js` that
    does not react to the music, with no error anywhere to say why. The
    revalidation is a 304 on loopback; it costs nothing.
    """
    resp = await handler(request)
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def build_app(runner, stage: dict, preview_audio: Path | None = None) -> web.Application:
    app = web.Application(middlewares=[_no_stale_static])
    app[RUNNER] = runner
    app[STAGE] = stage
    app[STAGE_BLOB] = stage_bytes(stage)
    app[PREVIEW_AUDIO] = preview_audio
    app[STARTED_AT] = time.time()
    app[CLIENT_LOG] = deque()

    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/stage", api_stage)
    app.router.add_get("/api/events", api_events)
    app.router.add_get("/api/audio/preview.wav", api_preview_audio)
    app.router.add_post("/api/play", api_play)
    app.router.add_post("/api/stop", api_stop)
    app.router.add_post("/api/estop", api_estop)
    app.router.add_post("/api/loop", api_loop)
    app.router.add_post("/api/volume", api_volume)
    app.router.add_post("/api/client-log", api_client_log)
    app.router.add_static("/static/", STATIC, name="static")

    async def _close_runner(_app: web.Application) -> None:
        await asyncio.get_running_loop().run_in_executor(None, runner.close)

    app.on_cleanup.append(_close_runner)
    return app


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "")


def serve(app: web.Application, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run until interrupted. Blocking; this is the end of the CLI path."""
    web.run_app(app, host=host, port=port, print=None, handle_signals=True)
