"""The control panel's HTTP surface.

create_app takes its paths explicitly so a test builds an app over tmp_path rather than over
the live config, the live credentials and the live logs.

Everything except /api/login, /api/session and the static files sits behind a session
dependency, and every mutating route additionally behind a CSRF dependency. A test enumerates
this app's own routes and asserts both -- that is what keeps it true as routes are added.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import conversation_archive
from webapp import (clips, config_store, conversations_view, credentials,
                    health as health_module, logs, registry, settings_api, vessels_view)
from webapp.auth import COOKIE_NAME, CSRF_HEADER, LoginThrottle, SessionStore, TooManyAttempts
from webapp.proxy_data import ProxyData
from webapp.supervisor import ProcessState, Supervisor, SupervisorError

STATIC_DIR = Path(__file__).resolve().parent / "static"


class LoginRequest(BaseModel):
    password: str


class SessionInfo(BaseModel):
    authenticated: bool
    csrf_token: str | None = None
    password_set: bool


def _is_secure(request: Request) -> bool:
    """TLS is terminated outside this app, so the forwarded header is the real signal."""
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return forwarded == "https" or request.url.scheme == "https"


def create_app(*, server_dir: Path, config_path: Path, credentials_path: Path,
               supervisor: Supervisor | None = None,
               proxy_data: ProxyData | None = None) -> FastAPI:
    # No docs_url, redoc_url or openapi_url: an unauthenticated schema of every route this
    # panel exposes is a gift to anyone who reaches the port.
    app = FastAPI(title="SDR# control panel", docs_url=None, redoc_url=None, openapi_url=None)

    sessions = SessionStore()
    throttle = LoginThrottle()

    def values() -> dict[str, str]:
        return config_store.load(config_path)

    paths = registry.resolve_paths(values(), server_dir)
    sup = supervisor or Supervisor(paths=paths, load_values=values)
    data = proxy_data or ProxyData(values)

    # -- guards ------------------------------------------------------------

    def require_session(request: Request):
        session = sessions.get(request.cookies.get(COOKIE_NAME))
        if session is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="not signed in")
        return session

    def require_csrf(request: Request, session=Depends(require_session)):
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not secrets.compare_digest(sent, session.csrf):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="missing or wrong CSRF token")
        return session

    def _spec_or_404(name: str):
        spec = registry.BY_NAME.get(name)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"no managed process named {name!r}")
        return spec

    # -- open routes -------------------------------------------------------

    open_routes = APIRouter()

    @open_routes.get("/api/session", response_model=SessionInfo)
    def session_probe(request: Request) -> SessionInfo:
        session = sessions.get(request.cookies.get(COOKIE_NAME))
        return SessionInfo(authenticated=session is not None,
                           csrf_token=session.csrf if session else None,
                           password_set=credentials.has_password(credentials_path))

    @open_routes.post("/api/login")
    def login(body: LoginRequest, request: Request, response: Response) -> dict:
        client = request.client.host if request.client else "unknown"
        try:
            throttle.check(client)
        except TooManyAttempts as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from None

        stored = credentials.load_hash(credentials_path)
        if not credentials.verify_password(stored, body.password):
            throttle.record_failure(client)
            # One message for a wrong password, a missing file and a damaged hash alike: a
            # login page must not tell an attacker which of those it is.
            raise HTTPException(status_code=401, detail="wrong password")

        throttle.record_success(client)
        session = sessions.create()
        response.set_cookie(COOKIE_NAME, session.token, httponly=True, samesite="strict",
                            secure=_is_secure(request), path="/")
        return {"csrf_token": session.csrf}

    # -- protected routes --------------------------------------------------

    guarded = APIRouter(dependencies=[Depends(require_session)])
    mutating = APIRouter(dependencies=[Depends(require_csrf)])

    @mutating.post("/api/logout")
    def logout(request: Request, response: Response) -> dict:
        token = request.cookies.get(COOKIE_NAME)
        if token:
            sessions.destroy(token)
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @guarded.get("/api/processes")
    def list_processes() -> dict:
        return {"processes": [state.model_dump() for state in sup.status_all()]}

    def _act(name: str, action: str) -> ProcessState:
        _spec_or_404(name)
        try:
            return getattr(sup, action)(name)
        except SupervisorError as exc:
            # 409, not 500: refusing to start something already running, or refusing to kill a
            # stranger's process, is a state conflict and the message is the whole point.
            raise HTTPException(status_code=409, detail=str(exc)) from None

    # Three routes rather than one /{action}: the action is part of the API surface, not a
    # parameter, and the test that enumerates mutating routes should see all three.
    @mutating.post("/api/processes/{name}/start", response_model=ProcessState)
    def start_process(name: str) -> ProcessState:
        return _act(name, "start")

    @mutating.post("/api/processes/{name}/stop", response_model=ProcessState)
    def stop_process(name: str) -> ProcessState:
        return _act(name, "stop")

    @mutating.post("/api/processes/{name}/restart", response_model=ProcessState)
    def restart_process(name: str) -> ProcessState:
        return _act(name, "restart")

    @guarded.get("/api/logs/{name}", response_model=logs.TailWindow)
    def read_log(name: str, offset: int | None = None,
                 limit: int = logs.DEFAULT_LIMIT) -> logs.TailWindow:
        _spec_or_404(name)
        path = sup.log_path(name)
        if path is None:
            return logs.TailWindow(path="", offset=0, next_offset=0, size=0,
                                   text="(this process has not written a log yet)")
        return logs.read_tail(path, offset=offset, limit=limit)

    @guarded.get("/api/health", response_model=health_module.Health)
    def read_health() -> health_module.Health:
        return health_module.health(values(), sup.paths)

    def _envelope(page, snap) -> dict:
        body = page.model_dump()
        body["snapshot"] = snap.model_dump()
        return body

    @guarded.get("/api/conversations")
    def read_conversations(identified: bool | None = None, channel: str | None = None,
                           text: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        records, snap = data.conversations()
        page = conversations_view.query(
            records, identified=identified, channel=channel, text=text,
            limit=limit, offset=offset)
        # The rows still come from the proxy's 15s snapshot; the comments are joined on here in
        # ONE query over the page's ids, never one per row -- this list is polled.
        found, comments_error = _comments_for_page([row["id"] for row in page.rows])
        for row in page.rows:
            comment = found.get(row["id"])
            row["has_comment"] = comment is not None
            row["truth"] = comment["truth"] if comment else None
        body = _envelope(page, snap)
        body["comments_error"] = comments_error
        return body

    def _archive_db():
        """Where the conversation archive lives, resolved per request.

        Per request rather than held, for the same reason _captures_root is: the path is a
        setting, and the Settings screen can change it while the panel is running.
        """
        return conversation_archive.resolve_db_path(
            values().get("CONVERSATIONS_DB"), server_dir)

    def _comments_for_page(ids: list[str]) -> tuple[dict[str, dict], str | None]:
        """Every comment for a page of ids, or a degraded empty result with a reason.

        The comment store is an enrichment on rows that already came from the healthy proxy
        snapshot, so a store that is locked, corrupted or sitting behind an unwritable
        directory must not take the whole screen down -- the same posture proxy_data.py takes
        for a fetch failure, and _archive_rows already takes on the write side. Exception,
        not sqlite3.Error: a bad configured path raises OSError from the mkdir inside
        conversation_archive.connect, and any of these must degrade the same way.
        """
        try:
            with conversation_archive.open_db(_archive_db()) as conn:
                return conversation_archive.comments_for(conn, ids), None
        except Exception as exc:
            return {}, f"the comment store could not be opened ({type(exc).__name__})"

    def _comment_for(conversation_id: str) -> tuple[dict | None, str | None]:
        """One conversation's comment, or a degraded None result with a reason. See
        _comments_for_page for why this must never raise into the route."""
        try:
            with conversation_archive.open_db(_archive_db()) as conn:
                return conversation_archive.get_comment(conn, conversation_id), None
        except Exception as exc:
            return None, f"the comment store could not be opened ({type(exc).__name__})"

    def _captures_root():
        """The configured capture directory, or None when it is unset or not there.

        Resolved per request rather than held: capture is turned on and off at the plugin, and
        the directory can appear, be pruned or be repointed while the panel keeps running.
        """
        configured = (values().get("CAPTURES_DIR") or "").strip()
        if not configured:
            return None
        root = Path(configured)
        return root if root.is_dir() else None

    @guarded.get("/api/conversations/{conversation_id:path}")
    def read_conversation(conversation_id: str) -> dict:
        records, _snap = data.conversations()
        for record in records:
            if conversations_view.conversation_id(record) == conversation_id:
                # The confirmation threshold comes from AIS_LIVE_MATCH_MAX_AGE_MIN, the same
                # setting the resolver uses to decide a live match is too stale to promote.
                # Read per request, because it can be changed in the Settings screen while the
                # panel is running.
                found = conversations_view.detail(
                    record, conversations_view.confirm_max_age_hours(values()))
                # Turns are matched to captured audio here rather than in conversations_view,
                # which is pure and reads nothing from disk. Every turn gets a clip or None, so
                # the UI shows a play button only where there is something to play.
                found["turns"] = clips.annotate(found.get("turns") or [],
                                                record.get("start"), _captures_root())
                found["comment"], found["comments_error"] = _comment_for(conversation_id)
                return found
        raise HTTPException(status_code=404, detail="no such conversation")

    @guarded.get("/api/clips/{day}/{clip}")
    def read_clip(day: str, clip: str) -> FileResponse:
        """The captured audio for one turn.

        clips.clip_path is the only thing that turns these two path segments into a filename,
        and it refuses anything that is not a bare date and a four-digit id inside the captures
        root. 404 for every failure alike: which of "capture was off", "the day was pruned" and
        "that is not a valid id" applies is not something this endpoint should disclose.
        """
        path = clips.clip_path(_captures_root(), day, clip)
        if path is None:
            raise HTTPException(status_code=404, detail="no audio for that turn")
        return FileResponse(path, media_type="audio/wav",
                            filename=f"{day}_{clip}.wav")

    @guarded.get("/api/vessels")
    def read_vessels(text: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        entries, snap = data.vessels()
        return _envelope(vessels_view.search(entries, text=text, limit=limit, offset=offset),
                         snap)

    @guarded.get("/api/vessels/{mmsi}")
    def read_vessel(mmsi: str) -> dict:
        entries, _snap = data.vessels()
        entry = vessels_view.detail(entries, mmsi)
        if entry is None:
            raise HTTPException(status_code=404, detail="no such vessel in the cache")
        records, conv_snap = data.conversations()
        entry["conversations"] = vessels_view.conversations_for(records, mmsi)
        # Nothing else polls conversations from this tab, so the 15s TTL is always expired
        # here -- every detail click is a live fetch, and it can fail. Without the snapshot
        # the UI cannot tell "this vessel has no conversations" from "the list could not be
        # fetched", and an empty list reads as the former either way.
        entry["conversations_snapshot"] = conv_snap.model_dump()
        return entry

    @guarded.get("/api/settings")
    def read_settings() -> dict:
        return settings_api.form(values())

    @mutating.post("/api/settings")
    def write_settings(body: dict[str, str]) -> dict:
        try:
            applied = settings_api.apply(values(), body)
        except settings_api.Invalid as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        # Only the keys that actually changed are written -- an untouched secret is never
        # round-tripped through this call, and config_store.save's merge leaves every other
        # stored value exactly as it was.
        if applied.changed:
            config_store.save(config_path, {key: applied.values[key] for key in applied.changed})
        return {"changed": sorted(applied.changed), "restart_needed": sorted(applied.restart_needed)}

    app.include_router(open_routes)
    app.include_router(guarded)
    app.include_router(mutating)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html",
                            headers={"Cache-Control": "no-cache"})

    class RevalidatingStatic(StaticFiles):
        """Serve the panel's own assets with `no-cache`.

        Not `no-store`: the file is still cached, but the browser must revalidate before using
        it, so an unchanged app.js costs a 304 and a changed one is picked up on an ordinary
        reload. Without this the page and its script have independent cache lifetimes, and a
        tab can end up running an older app.js against a newer index.html -- which happened on
        2026-08-19 and looked like a dead button rather than a stale file.
        """

        def file_response(self, *args, **kwargs):
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = "no-cache"
            return response

    app.mount("/static", RevalidatingStatic(directory=STATIC_DIR), name="static")
    return app
