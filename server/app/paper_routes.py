"""HTTP and WebSocket routes for Papering (app.paper)."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import auth, paper, paper_agent, store
from .store import GenerationMeta

router = APIRouter()

_MEDIA = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml"}


def _paper(gen_id: str) -> GenerationMeta:
    try:
        store.generation_dir(gen_id)
        meta = store.load_meta(gen_id)
    except (ValueError, KeyError) as exc:
        raise HTTPException(404, detail="Project not found") from exc
    if meta.kind != "paper" or not auth.owns(meta.owner):
        raise HTTPException(404, detail="Project not found")
    return meta


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except paper.PaperError as exc:
        raise HTTPException(exc.status, detail=str(exc)) from exc


def _touch(meta: GenerationMeta) -> None:
    store.save_meta(meta)  # bumps updated_at for "last modified"


def _build(gen_id: str, with_log: bool = False) -> dict[str, Any]:
    state = asdict(paper.build_state(gen_id))
    if not with_log:
        state.pop("log")
    return state


@router.post("/api/papers")
async def create_paper(
    title: str = Form(...), language: str = Form("en"), archive: UploadFile | None = File(default=None),
) -> JSONResponse:
    data = None
    if archive is not None and archive.filename:
        data = await archive.read(paper.PROJECT_LIMIT + 1)
        if len(data) > paper.PROJECT_LIMIT:
            raise HTTPException(413, detail="The zip is too large to import")
    meta = await run_in_threadpool(
        _call, paper.create, title, owner=auth.current_user.get() or "",
        language="zh" if language == "zh" else "en", archive=data,
    )
    return JSONResponse({"id": meta.id}, status_code=201)


@router.get("/api/papers/{gen_id}")
def get_paper(gen_id: str) -> dict[str, Any]:
    meta = _paper(gen_id)
    return {
        "meta": asdict(meta),
        "settings": paper.load_settings(gen_id),
        "entries": _call(paper.entries, gen_id),
        "build": {**_build(gen_id), "stale": paper.stale(gen_id)},
    }


@router.get("/api/papers/{gen_id}/source")
def get_source(gen_id: str, path: str = Query(...)) -> dict[str, str]:
    _paper(gen_id)
    return _call(paper.read_source, gen_id, path)


class SaveBody(BaseModel):
    text: str
    revision: str


@router.put("/api/papers/{gen_id}/source")
def put_source(gen_id: str, body: SaveBody, path: str = Query(...)) -> dict[str, str]:
    meta = _paper(gen_id)
    rev = _call(paper.save_source, gen_id, path, body.text, body.revision)
    _touch(meta)
    return {"revision": rev}


class NewFileBody(BaseModel):
    path: str
    directory: bool = False


@router.post("/api/papers/{gen_id}/files", status_code=201)
def new_file(gen_id: str, body: NewFileBody) -> Response:
    meta = _paper(gen_id)
    _call(paper.new_file, gen_id, body.path, body.directory)
    _touch(meta)
    return Response(status_code=201)


@router.post("/api/papers/{gen_id}/upload", status_code=201)
async def upload(gen_id: str, folder: str = Form(""), paths: list[str] = Form(default=[]),
                 files: list[UploadFile] = File(...)) -> dict[str, Any]:
    """Each file goes to `paths[i]` when given (folder import keeps its tree),
    else to `folder/<file name>`. An existing file is replaced."""
    meta = _paper(gen_id)
    saved, errors = [], []
    for i, upload_file in enumerate(files):
        name = paths[i] if i < len(paths) and paths[i] else "/".join(p for p in (folder.strip("/"), upload_file.filename or "") if p)
        data = await upload_file.read(paper.UPLOAD_LIMIT + 1)
        try:
            await run_in_threadpool(paper.write_upload, gen_id, name, data, replace=True)
            saved.append(name)
        except paper.PaperError as exc:
            errors.append(f"{name}: {exc}")
    _touch(meta)
    return {"saved": saved, "errors": errors}


@router.delete("/api/papers/{gen_id}/files", status_code=204)
def delete_file(gen_id: str, path: str = Query(...)) -> Response:
    meta = _paper(gen_id)
    _call(paper.delete_path, gen_id, path)
    _touch(meta)
    return Response(status_code=204)


class RenameBody(BaseModel):
    path: str
    to: str


@router.post("/api/papers/{gen_id}/rename", status_code=204)
def rename(gen_id: str, body: RenameBody) -> Response:
    meta = _paper(gen_id)
    _call(paper.rename_path, gen_id, body.path, body.to)
    _touch(meta)
    return Response(status_code=204)


@router.get("/api/papers/{gen_id}/asset")
def asset(gen_id: str, path: str = Query(...)) -> Response:
    _paper(gen_id)
    target = _call(paper.resolve, gen_id, path)
    data = _call(paper._read_regular, target, paper.UPLOAD_LIMIT)
    media = _MEDIA.get(target.suffix.lower(), "application/octet-stream")
    headers = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"}
    if media == "application/octet-stream":
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{_quote(target.name)}"
    return Response(data, media_type=media, headers=headers)


class SettingsBody(BaseModel):
    engine: str
    main: str


@router.put("/api/papers/{gen_id}/settings")
def put_settings(gen_id: str, body: SettingsBody) -> dict[str, str]:
    _paper(gen_id)
    return _call(paper.save_settings, gen_id, body.engine, body.main)


@router.post("/api/papers/{gen_id}/compile")
def compile_paper(gen_id: str) -> dict[str, Any]:
    _paper(gen_id)
    _call(paper.compile_project, gen_id)
    return _build(gen_id, with_log=True)


@router.get("/api/papers/{gen_id}/build")
def get_build(gen_id: str) -> dict[str, Any]:
    _paper(gen_id)
    return _build(gen_id, with_log=True)


@router.get("/api/papers/{gen_id}/pdf")
def get_pdf(gen_id: str) -> FileResponse:
    meta = _paper(gen_id)
    pdf = paper.output_pdf(gen_id)
    if not pdf.is_file():
        raise HTTPException(404, detail="Compile the project first")
    return FileResponse(pdf, media_type="application/pdf", filename=_name(meta, ".pdf"),
                        content_disposition_type="inline")


@router.get("/api/papers/{gen_id}/forward")
def forward(gen_id: str, path: str = Query(...), line: int = Query(..., ge=1, le=2_000_000)) -> dict[str, float]:
    _paper(gen_id)
    return _call(paper.forward, gen_id, path, line)


@router.get("/api/papers/{gen_id}/inverse")
def inverse(gen_id: str, page: int = Query(..., ge=1, le=100_000), x: float = Query(..., ge=0, le=100_000),
            y: float = Query(..., ge=0, le=100_000)) -> dict[str, Any]:
    _paper(gen_id)
    return _call(paper.inverse, gen_id, page, x, y)


@router.get("/api/papers/{gen_id}/project.zip")
def project_zip(gen_id: str) -> Response:
    meta = _paper(gen_id)
    data = _call(paper.export_zip, gen_id)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_quote(_name(meta, '.zip'))}"})


def _name(meta: GenerationMeta, ext: str) -> str:
    stem = "".join(c for c in (meta.title or "paper") if c not in '<>:"/\\|?*' and ord(c) >= 32).strip()[:120]
    return (stem or "paper") + ext


def _quote(name: str) -> str:
    from urllib.parse import quote

    return quote(name, safe="")


@router.websocket("/ws/papers/{gen_id}")
async def watch(websocket: WebSocket, gen_id: str) -> None:
    """Pushes {files, build} whenever the project's files or build change."""
    if auth.enabled():
        user = auth.session_user(websocket.cookies.get(auth.COOKIE))
        if user is None:
            await websocket.close(code=1008, reason="Sign in first")
            return
        auth.current_user.set(user)
    try:
        _paper(gen_id)
    except HTTPException:
        await websocket.close(code=1008, reason="Project not found")
        return
    await websocket.accept()
    last: dict[str, Any] = {}
    try:
        while True:
            files = await run_in_threadpool(paper.fingerprint, gen_id)
            build = _build(gen_id)
            agent = {"running": paper_agent.running(gen_id), "version": paper_agent.version(gen_id)}
            state = {"files": files, "build": build, "agent": agent}
            if state != last:
                await websocket.send_json(state)
                last = state
            # Waiting on receive (not sleep) notices a closed page even while
            # nothing changes; the page never sends anything.
            try:
                busy = build["running"] or agent["running"]
                await asyncio.wait_for(websocket.receive_text(), timeout=0.5 if busy else 1.0)
            except asyncio.TimeoutError:
                pass
    except (WebSocketDisconnect, RuntimeError):
        return


# ---- Agent ----


def _agent(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except paper_agent.AgentError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


@router.get("/api/papers/{gen_id}/agent")
def agent_state(gen_id: str) -> dict[str, Any]:
    _paper(gen_id)
    return {
        **paper_agent.options(),
        "sessions": paper_agent.list_sessions(gen_id),
        "running": paper_agent.running(gen_id),
        "version": paper_agent.version(gen_id),
    }


@router.get("/api/papers/{gen_id}/agent/sessions/{sid}")
def agent_transcript(gen_id: str, sid: str) -> dict[str, Any]:
    _paper(gen_id)
    return {"items": _agent(paper_agent.transcript, gen_id, sid), "version": paper_agent.version(gen_id)}


class AgentBody(BaseModel):
    kind: str
    prompt: str
    session: str = ""
    context: dict[str, Any] = {}
    model: str = ""


@router.post("/api/papers/{gen_id}/agent", status_code=202)
def agent_start(gen_id: str, body: AgentBody) -> dict[str, str]:
    _paper(gen_id)
    sid = _agent(paper_agent.start, gen_id, body.kind, body.prompt, body.session, body.context, body.model)
    return {"session": sid}


@router.post("/api/papers/{gen_id}/agent/stop", status_code=204)
def agent_stop(gen_id: str) -> Response:
    _paper(gen_id)
    paper_agent.stop(gen_id)
    return Response(status_code=204)


@router.delete("/api/papers/{gen_id}/agent/sessions/{sid}", status_code=204)
def agent_delete_session(gen_id: str, sid: str) -> Response:
    _paper(gen_id)
    _agent(paper_agent.delete_session, gen_id, sid)
    return Response(status_code=204)


@router.post("/api/agent/codex/login")
def codex_login() -> dict[str, Any]:
    return _agent(paper_agent.start_codex_login)


@router.get("/api/agent/codex/login")
def codex_login_state() -> dict[str, Any]:
    return {**paper_agent.codex_login_state(), "signed_in": paper_agent.codex_signed_in()}


@router.delete("/api/agent/codex/login", status_code=204)
def codex_login_cancel() -> Response:
    paper_agent.cancel_codex_login()
    return Response(status_code=204)


@router.delete("/api/agent/codex", status_code=204)
def codex_sign_out() -> Response:
    paper_agent.sign_out_codex()
    return Response(status_code=204)
