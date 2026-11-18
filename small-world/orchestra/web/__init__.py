"""The web console: play / stop / loop / volume, plus the show on screen.

Three pieces, deliberately separable:

* `export`  -- reshapes `build/<song>/*.program.json.gz` into one file a browser
               can fetch. Nothing is recomputed; the twin on screen plays the
               same frames the robots play.
* `runner`  -- owns the performance. One thread, one state machine, driven by
               HTTP instead of by keypresses.
* `server`  -- aiohttp. Routes, Server-Sent Events, static files.

`orchestra show run` is still the production path for a performance someone is
watching from a terminal. This is the same lifecycle behind a screen anyone can
operate, which is a different job and so a different front end.
"""

from .export import STAGE_FPS, stage_data, write_stage
from .runner import FleetBackend, RehearsalBackend, ShowRunner, State
from .server import build_app, serve

__all__ = [
    "STAGE_FPS",
    "FleetBackend",
    "RehearsalBackend",
    "ShowRunner",
    "State",
    "build_app",
    "serve",
    "stage_data",
    "write_stage",
]
