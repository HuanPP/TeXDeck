from __future__ import annotations

import json
import shutil
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from . import deck, pdf, store, tasks
from .config import settings
from .deck import CompileError, DeckError, DeckRequest
from .edit import (
    EditError,
    build_frame_edit_messages,
    extract_frame,
    parse_frames,
    replace_frame,
    resolve_frame_pages,
)
from . import auth, chatgpt, claude_code
from .llm import (
    LlmError,
    ProgressCallback,
    UsageTally,
    active_model,
    create_client,
    ensure_model_ready,
    model_status,
    remove_api_config,
    save_api_config,
    select_provider,
    set_model,
)
from .pptx_export import EXPORT_VERSION as PPTX_EXPORT_VERSION
from .pptx_export import PptxExportError, pdf_to_pptx
from .store import GenerationMeta


class FrameEditBody(BaseModel):
    instruction: str


class RefineBody(BaseModel):
    instruction: str


class CallbackUrlBody(BaseModel):
    url: str


class ClaudeCodeBody(BaseModel):
    code: str


class SignInBody(BaseModel):
    username: str
    password: str


class ProviderBody(BaseModel):
    provider: str


class ModelBody(BaseModel):
    provider: str
    model: str


class ApiKeyBody(BaseModel):
    api_key: str = ""
    base_url: str
    model: str


# Hard cap on PDFs per generation; the combined 200-page cap is enforced in pdf.load_pdfs.
MAX_UPLOAD_FILES = 10


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    # Jobs live only in this process; anything still "running" on disk was
    # killed by the previous shutdown and will never finish.
    store.mark_interrupted_generations()
    yield


app = FastAPI(title="TeXDeck", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Reachable without a session: checking and starting one.
_OPEN_API_PATHS = {"/api/auth/session"}


@app.middleware("http")
async def require_sign_in(request: Request, call_next: Any) -> Response:
    path = request.url.path
    if path.startswith("/api/") and path not in _OPEN_API_PATHS and auth.enabled():
        if auth.session_user(request.cookies.get(auth.COOKIE)) is None:
            return JSONResponse({"detail": "Sign in first"}, status_code=401)
    response = await call_next(request)
    # PDF/TeX/frames change in place after every edit; without this the browser
    # heuristically reuses its stale copy on reload. ETag keeps revalidation cheap.
    if path.startswith("/api/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response


def _load(gen_id: str) -> GenerationMeta:
    try:
        store.generation_dir(gen_id)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    try:
        return store.load_meta(gen_id)
    except KeyError as e:
        raise HTTPException(404, detail=f"Generation not found: {gen_id}") from e


def _file(gen_id: str, name: str, missing: str) -> Path:
    p = store.generation_dir(gen_id) / name
    if not p.is_file():
        raise HTTPException(404, detail=missing)
    return p


def _sanitize(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    n = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        parts: list[Any] = []
        for part in content:
            img = isinstance(part, dict) and part.get("type") == "image_url"
            if img and isinstance(part.get("image_url"), dict):
                n += 1
                parts.append({"type": "image_url", "image_url": {"url": f"<image:page_{n:03d}>"}})
            else:
                parts.append(part)
        out.append({**msg, "content": parts})
    return out


def _dump_llm(gdir: Path, stem: str, messages: list[dict[str, Any]], response: str) -> None:
    d = gdir / "llm"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}_prompt.json").write_text(
        json.dumps(_sanitize(messages), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (d / f"{stem}_response.txt").write_text(response, encoding="utf-8")


def _error_kind(exc: BaseException) -> str:
    return "model" if isinstance(exc, LlmError) else ""


def _fail(meta: GenerationMeta, exc: BaseException) -> None:
    meta.status = "failed"
    meta.error = str(exc)
    meta.error_kind = _error_kind(exc)
    store.save_meta(meta)


def _edit_fail(meta: GenerationMeta, exc: BaseException) -> None:
    # Edit rolled back; finished PDF still usable; keep status (usually succeeded)
    meta.error = f"This edit failed and was rolled back: {exc}"
    meta.error_kind = _error_kind(exc)
    store.save_meta(meta)


def _refine_fail(meta: GenerationMeta, exc: BaseException) -> None:
    # Refine rolled back; finished PDF still usable; keep status (usually succeeded)
    meta.error = f"This refine failed and was rolled back: {exc}"
    meta.error_kind = _error_kind(exc)
    store.save_meta(meta)


def _require_model() -> None:
    try:
        ensure_model_ready()
    except LlmError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


def _apply_usage(meta: GenerationMeta, usage: UsageTally) -> None:
    """Fold one job's token tally into the meta (caller persists via save_meta)."""
    if usage.calls == 0:
        return
    meta.llm_model = active_model()
    meta.llm_calls += usage.calls
    meta.llm_prompt_tokens += usage.prompt_tokens
    meta.llm_completion_tokens += usage.completion_tokens


def _run_generation(gen_id: str, progress: ProgressCallback) -> None:
    meta = store.load_meta(gen_id)
    gdir = store.generation_dir(gen_id)
    usage = UsageTally()
    try:
        template = deck.get_template(meta.template_key)
        deck.copy_template_bundle(template, gdir)
        progress("Preparing template", 2)
        documents: list[pdf.PdfDocument] = []
        source_text = ""
        if meta.source_kind == "pdf":
            paths = sorted(gdir.glob("source_*.pdf"))
            if not paths:
                raise RuntimeError("Uploaded PDFs are missing")
            if len(paths) != len(meta.source_names):
                raise RuntimeError("Source file count does not match metadata")

            def pdf_progress(stage: str, percent: int) -> None:
                progress(stage, 5 + percent * 40 // 100)

            documents = pdf.load_pdfs(
                list(zip(paths, meta.source_names, strict=True)),
                gdir,
                progress=pdf_progress,
                on_usage=usage.add,
            )
            meta.page_count = sum(len(d.pages) for d in documents)
            store.save_meta(meta)
        else:
            # Pasted text lives on disk so the generation stays re-runnable
            # after a server restart (and regenerate can copy it).
            source_path = gdir / "source.txt"
            if not source_path.is_file():
                raise RuntimeError("Pasted text source is missing")
            source_text = source_path.read_text(encoding="utf-8")
        request = DeckRequest(
            template_key=template.key,
            title=meta.title,
            author=meta.author,
            institute=meta.institute,
            instruction=meta.instruction,
            source_text=source_text,
            language=meta.language,
        )
        messages = deck.build_generation_messages(template, request, documents)
        progress("Generating slides with the model", 50)
        client = create_client(on_usage=usage.add)
        response = client.complete(messages)
        _dump_llm(gdir, "generate", messages, response)
        progress("Validating model output", 70)
        try:
            tex = deck.extract_tex(response, template)
        except DeckError as first_err:
            retry = messages + [
                {"role": "assistant", "content": response},
                {
                    "role": "user",
                    "content": (
                        f"Previous output was invalid: {first_err}\n"
                        "Re-output a complete, compilable XeLaTeX Beamer document. "
                        "No explanation, no markdown fences."
                    ),
                },
            ]
            response = client.complete(retry)
            _dump_llm(gdir, "generate_retry", retry, response)
            tex = deck.extract_tex(response, template)
        tex_path = gdir / "main.tex"
        tex_path.write_text(tex, encoding="utf-8")

        def compile_progress(_stage: str, percent: int) -> None:
            progress("Compiling", 75 + percent * 20 // 100)

        deck.compile_tex(
            tex_path,
            client=client,
            progress=compile_progress,
            llm_dump_dir=gdir / "llm",
        )
        final_tex = tex_path.read_text(encoding="utf-8")
        meta.frame_count = len(parse_frames(final_tex))
        if not meta.title:
            meta.title = deck.extract_title(final_tex)
        _apply_usage(meta, usage)
        meta.status, meta.error = "succeeded", ""
        store.save_meta(meta)
        if not settings.keep_page_renders:
            # Page renders only feed the LLM; on success they are dead weight.
            # Failures keep them for debugging.
            shutil.rmtree(gdir / "pages", ignore_errors=True)
            shutil.rmtree(gdir / "thumbs", ignore_errors=True)
        progress("Done", 100)
    except Exception as exc:
        _apply_usage(meta, usage)
        _fail(meta, exc)
        raise


def _run_edit(gen_id: str, index: int, instruction: str, progress: ProgressCallback) -> None:
    meta = store.load_meta(gen_id)
    gdir = store.generation_dir(gen_id)
    tex_path = gdir / "main.tex"
    usage = UsageTally()
    try:
        progress("Preparing edit", 5)
        original = tex_path.read_text(encoding="utf-8")
        frame = next((f for f in parse_frames(original) if f.index == index), None)
        if frame is None:
            raise RuntimeError(f"Slide index not found: {index}")
        template = deck.get_template(meta.template_key)
        messages = build_frame_edit_messages(frame, instruction, template.main_tex)
        progress("Editing one slide with the model", 30)
        client = create_client(on_usage=usage.add)
        response = client.complete(messages)
        _dump_llm(gdir, f"edit_{index}", messages, response)
        new_tex = replace_frame(original, frame, extract_frame(response))
        edit_dir = store.next_edit_dir(gen_id)
        (edit_dir / "main.tex").write_text(new_tex, encoding="utf-8")
        (edit_dir / "edit.json").write_text(
            json.dumps(
                {
                    "instruction": instruction,
                    "index": index,
                    "time": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        tex_path.write_text(new_tex, encoding="utf-8")
        progress("Compiling", 70)
        try:
            deck.compile_tex(tex_path, client=client, llm_dump_dir=gdir / "llm")
        except Exception as compile_exc:
            tex_path.write_text(original, encoding="utf-8")
            try:
                deck.compile_tex(tex_path, client=None)
            except Exception as restore_exc:
                raise RuntimeError(
                    f"Compile failed after edit: {compile_exc}; "
                    f"restore of original also failed: {restore_exc}"
                ) from compile_exc
            raise
        meta.edit_count += 1
        _apply_usage(meta, usage)
        meta.status, meta.error = "succeeded", ""
        store.save_meta(meta)
        progress("Done", 100)
    except Exception as exc:
        _apply_usage(meta, usage)
        _edit_fail(meta, exc)
        raise


def _run_refine(gen_id: str, instruction: str, progress: ProgressCallback) -> None:
    meta = store.load_meta(gen_id)
    gdir = store.generation_dir(gen_id)
    tex_path = gdir / "main.tex"
    usage = UsageTally()
    try:
        progress("Preparing refine", 5)
        original = tex_path.read_text(encoding="utf-8")
        template = deck.get_template(meta.template_key)
        stem = f"refine_{meta.refine_count + 1}"
        messages = [
            {
                "role": "system",
                "content": (
                    "You are revising a complete XeLaTeX Beamer deck. "
                    "Output only the full revised, compilable document. "
                    "No explanation, no markdown fences."
                ),
            },
            {"role": "user", "content": template.main_tex},
            {"role": "user", "content": original},
            {"role": "user", "content": "Revision request:\n" + instruction},
        ]
        progress("Revising the whole deck with the model", 30)
        client = create_client(on_usage=usage.add)
        response = client.complete(messages)
        _dump_llm(gdir, stem, messages, response)
        progress("Validating model output", 55)
        try:
            tex = deck.extract_tex(response, template)
        except DeckError as first_err:
            retry = messages + [
                {"role": "assistant", "content": response},
                {
                    "role": "user",
                    "content": (
                        f"Previous output was invalid: {first_err}\n"
                        "Re-output a complete, compilable XeLaTeX Beamer document. "
                        "No explanation, no markdown fences."
                    ),
                },
            ]
            response = client.complete(retry)
            _dump_llm(gdir, f"{stem}_retry", retry, response)
            tex = deck.extract_tex(response, template)
        edit_dir = store.next_edit_dir(gen_id)
        (edit_dir / "main.tex.bak").write_text(original, encoding="utf-8")
        (edit_dir / "refine.json").write_text(
            json.dumps(
                {
                    "instruction": instruction,
                    "time": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        tex_path.write_text(tex, encoding="utf-8")
        progress("Compiling", 70)
        try:
            deck.compile_tex(tex_path, client=client, llm_dump_dir=gdir / "llm")
        except Exception as compile_exc:
            tex_path.write_text(original, encoding="utf-8")
            try:
                deck.compile_tex(tex_path, client=None)
            except Exception as restore_exc:
                raise RuntimeError(
                    f"Compile failed after refine: {compile_exc}; "
                    f"restore of original also failed: {restore_exc}"
                ) from compile_exc
            raise
        meta.refine_count += 1
        meta.frame_count = len(parse_frames(tex_path.read_text(encoding="utf-8")))
        _apply_usage(meta, usage)
        meta.status, meta.error = "succeeded", ""
        store.save_meta(meta)
        progress("Done", 100)
    except Exception as exc:
        _apply_usage(meta, usage)
        _refine_fail(meta, exc)
        raise


def _plain(gen_id: str, name: str, missing: str) -> FileResponse:
    _load(gen_id)
    return FileResponse(_file(gen_id, name, missing), media_type="text/plain; charset=utf-8", filename=name)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _session_status(request: Request) -> dict[str, Any]:
    return {"enabled": auth.enabled(), "user": auth.session_user(request.cookies.get(auth.COOKIE))}


@app.get("/api/auth/session")
def api_session(request: Request) -> dict[str, Any]:
    return _session_status(request)


@app.post("/api/auth/session")
def api_sign_in(body: SignInBody, request: Request) -> JSONResponse:
    # Only Cloudflare reaches the deployed origin, and it sets CF-Connecting-IP.
    client = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "")
    try:
        token = auth.login(body.username, body.password, client)
    except auth.AuthError as exc:
        status = 429 if "Too many" in str(exc) else 401
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    response = JSONResponse({"enabled": True, "user": body.username.strip()})
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        auth.COOKIE, token, max_age=auth.SESSION_SECONDS, httponly=True, secure=https, samesite="lax",
    )
    return response


@app.delete("/api/auth/session")
def api_sign_out() -> JSONResponse:
    response = JSONResponse({"enabled": auth.enabled(), "user": None})
    response.delete_cookie(auth.COOKIE)
    return response


@app.get("/api/model")
def apimodel_status() -> dict[str, Any]:
    return model_status()


@app.put("/api/model/provider")
def api_select_provider(body: ProviderBody) -> dict[str, Any]:
    try:
        select_provider(body.provider)
    except LlmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return model_status()


@app.put("/api/model/model")
def api_set_model(body: ModelBody) -> dict[str, Any]:
    try:
        set_model(body.provider, body.model)
    except LlmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return model_status()


@app.get("/api/model/chatgpt/models")
def api_chatgpt_models() -> dict[str, list[dict[str, str]]]:
    try:
        return {"models": chatgpt.list_models()}
    except chatgpt.ChatGptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/model/api")
def api_save_api_key(body: ApiKeyBody) -> dict[str, Any]:
    try:
        save_api_config(body.api_key, body.base_url, body.model)
    except LlmError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return model_status()


@app.delete("/api/model/api")
def api_remove_api_key() -> dict[str, Any]:
    remove_api_config()
    return model_status()


@app.post("/api/auth/chatgpt/login")
def api_chatgpt_login() -> dict[str, str]:
    return {"url": chatgpt.start_login()}


@app.post("/api/auth/chatgpt/callback")
def api_chatgpt_callback(body: CallbackUrlBody) -> dict[str, Any]:
    """Paste-back path for when the browser cannot reach the loopback listener."""
    try:
        chatgpt.complete_login_from_url(body.url)
    except chatgpt.ChatGptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return model_status()


@app.delete("/api/auth/chatgpt")
def api_chatgpt_logout() -> dict[str, Any]:
    chatgpt.sign_out()
    return model_status()


@app.post("/api/auth/claude/login")
def api_claude_login() -> dict[str, Any]:
    """Connect Claude Code; "url" is empty when its CLI was already signed in."""
    try:
        url = claude_code.start_login()
    except claude_code.ClaudeCodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"url": url, **model_status()}


@app.post("/api/auth/claude/code")
def api_claude_code(body: ClaudeCodeBody) -> dict[str, Any]:
    try:
        claude_code.submit_code(body.code)
    except claude_code.ClaudeCodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return model_status()


@app.delete("/api/auth/claude/login")
def api_claude_cancel() -> dict[str, Any]:
    claude_code.cancel_login()
    return model_status()


@app.get("/api/templates")
def api_list_templates() -> list[dict[str, str]]:
    return [{"key": t.key, "name": t.name} for t in deck.list_templates()]


@app.get("/api/templates/{key}/preview.png")
def api_template_preview(key: str) -> FileResponse:
    try:
        template = deck.get_template(key)
    except ValueError as e:
        raise HTTPException(404, detail=str(e)) from e
    try:
        path = deck.render_template_preview(template)
    except CompileError as e:
        tail = e.log[-2000:] if e.log else ""
        detail = f"Template preview build failed: {e}"
        if tail:
            detail += f"\n{tail}"
        raise HTTPException(503, detail=detail) from e
    # Content hash lives server-side in the filename; clients must revalidate.
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.get("/api/generations")
def api_list_generations() -> list[dict[str, Any]]:
    return [asdict(m) for m in store.list_generations()]


async def _read_upload_capped(file: UploadFile) -> bytes:
    """Read one upload in 1MB chunks; the size cap applies per file."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1 << 20):
        total += len(chunk)
        if total > settings.max_upload_bytes:
            limit_mb = settings.max_upload_bytes // (1024 * 1024)
            raise HTTPException(
                413, detail=f"Uploaded PDF exceeds the {limit_mb} MB limit: {file.filename}"
            )
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/api/generations")
async def api_create_generation(
    template_key: str = Form(...),
    title: str = Form(""),
    language: str = Form("source"),
    instruction: str = Form(""),
    source_text: str = Form(""),
    author: str = Form(""),
    institute: str = Form(""),
    files: list[UploadFile] = File(default=[]),
) -> JSONResponse:
    try:
        template = deck.get_template(template_key)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    if language not in deck.LANGUAGE_RULES:
        raise HTTPException(400, detail=f"Unknown slide language: {language}")
    _require_model()
    uploads = [f for f in files if f.filename]
    if len(uploads) > MAX_UPLOAD_FILES:
        raise HTTPException(400, detail=f"At most {MAX_UPLOAD_FILES} PDF files per generation")
    if uploads:
        contents: list[bytes] = []
        for f in uploads:
            data = await _read_upload_capped(f)
            if not data:
                raise HTTPException(400, detail=f"Uploaded PDF is empty: {f.filename}")
            contents.append(data)
        kind, names, text_body = "pdf", [f.filename or "source.pdf" for f in uploads], ""
    elif source_text.strip():
        contents, kind, names, text_body = [], "text", [], source_text
    else:
        raise HTTPException(400, detail="Upload at least one PDF or provide pasted text")
    meta = store.new_generation(
        title=title.strip(),
        template_key=template.key,
        template_name=template.name,
        instruction=instruction or "",
        source_kind=kind,
        source_names=names,
        author=(author or "").strip(),
        institute=(institute or "").strip(),
        language=language,
    )
    gdir = store.generation_dir(meta.id)
    # source_XX.pdf index encodes reading order; original names live in meta.source_names
    for i, data in enumerate(contents, start=1):
        (gdir / f"source_{i:02d}.pdf").write_bytes(data)
    if kind == "text":
        # Persisted so the job (and any later regenerate) reads it from disk.
        (gdir / "source.txt").write_text(text_body, encoding="utf-8")
    gen_id = meta.id

    def job(progress: ProgressCallback) -> None:
        _run_generation(gen_id, progress)

    tasks.start(gen_id, job)
    return JSONResponse({"id": gen_id}, status_code=202)


@app.get("/api/generations/{gen_id}")
def api_get_generation(gen_id: str) -> dict[str, Any]:
    return asdict(_load(gen_id))


@app.delete("/api/generations/{gen_id}", status_code=204)
def api_delete_generation(gen_id: str) -> Response:
    _load(gen_id)
    if tasks.is_running(gen_id):
        raise HTTPException(409, detail="Cannot delete while a job is running for this generation")
    try:
        store.delete_generation(gen_id)
    except KeyError as e:
        raise HTTPException(404, detail=f"Generation not found: {gen_id}") from e
    return Response(status_code=204)


@app.get("/api/generations/{gen_id}/pdf")
def api_get_pdf(gen_id: str) -> FileResponse:
    _load(gen_id)
    return FileResponse(
        _file(gen_id, "main.pdf", "PDF not generated yet"),
        media_type="application/pdf",
        filename="main.pdf",
        content_disposition_type="inline",
    )


@app.get("/api/generations/{gen_id}/tex")
def api_get_tex(gen_id: str) -> FileResponse:
    return _plain(gen_id, "main.tex", "TeX not generated yet")


@app.get("/api/generations/{gen_id}/log")
def api_get_log(gen_id: str) -> FileResponse:
    return _plain(gen_id, "main.log", "Compile log not found")


@app.get("/api/generations/{gen_id}/bundle.zip")
def api_get_bundle(gen_id: str) -> FileResponse:
    meta = _load(gen_id)
    try:
        path = store.bundle_zip(gen_id)
    except KeyError as e:
        raise HTTPException(404, detail=f"Generation not found: {gen_id}") from e
    return FileResponse(
        path, media_type="application/zip", filename=(meta.title or gen_id).replace("/", "_") + ".zip"
    )


_pptx_lock = threading.Lock()


@app.get("/api/generations/{gen_id}/deck.pptx")
def api_get_pptx(gen_id: str) -> FileResponse:
    meta = _load(gen_id)
    pdf_path = _file(gen_id, "main.pdf", "PDF not generated yet")
    # Versioned name: an improved exporter must not serve decks cached by the old one.
    out = pdf_path.with_name(f"main.v{PPTX_EXPORT_VERSION}.pptx")
    # Built on first download and rebuilt whenever an edit recompiles the PDF.
    with _pptx_lock:
        if not out.is_file() or out.stat().st_mtime < pdf_path.stat().st_mtime:
            try:
                pdf_to_pptx(pdf_path, out)
            except PptxExportError as e:
                raise HTTPException(422, detail=str(e)) from e
            for old in pdf_path.parent.glob("main*.pptx"):
                if old != out:
                    old.unlink(missing_ok=True)
    return FileResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=(meta.title or gen_id).replace("/", "_") + ".pptx",
    )


@app.get("/api/generations/{gen_id}/frames")
def api_list_frames(gen_id: str) -> list[dict[str, Any]]:
    _load(gen_id)
    tex_path = _file(gen_id, "main.tex", "TeX not generated yet")
    try:
        frames = resolve_frame_pages(parse_frames(tex_path.read_text(encoding="utf-8")), tex_path)
    except EditError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return [{"index": f.index, "title": f.title, "page": f.page, "tex": f.tex} for f in frames]


@app.post("/api/generations/{gen_id}/frames/{index}/edit")
def api_edit_frame(gen_id: str, index: int, body: FrameEditBody) -> Response:
    _load(gen_id)
    if not body.instruction.strip():
        raise HTTPException(400, detail="Edit instruction must not be empty")
    tex_path = _file(gen_id, "main.tex", "TeX not generated yet")
    try:
        frames = parse_frames(tex_path.read_text(encoding="utf-8"))
    except EditError as e:
        raise HTTPException(422, detail=str(e)) from e
    if not any(f.index == index for f in frames):
        raise HTTPException(400, detail=f"Slide index not found: {index}")
    _require_model()
    instruction = body.instruction

    def job(progress: ProgressCallback) -> None:
        _run_edit(gen_id, index, instruction, progress)

    try:
        tasks.start(gen_id, job)
    except tasks.TaskBusy as e:
        raise HTTPException(409, detail=str(e)) from e
    return Response(status_code=202)


@app.post("/api/generations/{gen_id}/refine")
def api_refine_deck(gen_id: str, body: RefineBody) -> Response:
    _load(gen_id)
    if not body.instruction.strip():
        raise HTTPException(400, detail="Refine instruction must not be empty")
    _file(gen_id, "main.tex", "TeX not generated yet")
    _require_model()
    instruction = body.instruction

    def job(progress: ProgressCallback) -> None:
        _run_refine(gen_id, instruction, progress)

    try:
        tasks.start(gen_id, job)
    except tasks.TaskBusy as e:
        raise HTTPException(409, detail=str(e)) from e
    return Response(status_code=202)


@app.post("/api/generations/{gen_id}/regenerate")
def api_regenerate(gen_id: str) -> JSONResponse:
    old_meta = _load(gen_id)
    _require_model()
    old_dir = store.generation_dir(gen_id)
    if old_meta.source_kind == "pdf":
        sources = sorted(old_dir.glob("source_*.pdf"))
        if not sources or len(sources) != len(old_meta.source_names):
            raise HTTPException(
                400, detail="Source PDFs of this generation are missing; cannot regenerate"
            )
    else:
        sources = [old_dir / "source.txt"]
        if not sources[0].is_file():
            raise HTTPException(
                400, detail="Pasted text of this generation is missing; cannot regenerate"
            )
    meta = store.new_generation(
        title=old_meta.title,
        template_key=old_meta.template_key,
        template_name=old_meta.template_name,
        instruction=old_meta.instruction,
        source_kind=old_meta.source_kind,
        source_names=list(old_meta.source_names),
        author=old_meta.author,
        institute=old_meta.institute,
        language=old_meta.language,
    )
    new_dir = store.generation_dir(meta.id)
    for source in sources:
        shutil.copy2(source, new_dir / source.name)
    new_id = meta.id

    def job(progress: ProgressCallback) -> None:
        _run_generation(new_id, progress)

    tasks.start(new_id, job)
    return JSONResponse({"id": new_id}, status_code=202)


@app.websocket("/ws/generations/{gen_id}")
async def ws_generation(websocket: WebSocket, gen_id: str) -> None:
    if auth.enabled() and auth.session_user(websocket.cookies.get(auth.COOKIE)) is None:
        await websocket.close(code=1008, reason="Sign in first")
        return
    try:
        store.generation_dir(gen_id)
    except ValueError:
        await websocket.close(code=1008, reason="Invalid generation ID")
        return
    await websocket.accept()
    if tasks.get(gen_id) is None:
        # No live job in this process: answer from disk instead of hanging forever
        # (unknown id, or a job the last server run finished/lost).
        try:
            meta = store.load_meta(gen_id)
        except KeyError:
            await websocket.close(code=1008, reason=f"Generation not found: {gen_id}")
            return
        status = meta.status if meta.status in ("succeeded", "failed") else "failed"
        error = meta.error or ("Job is no longer running" if status == "failed" else "")
        await websocket.send_json(
            asdict(
                tasks.TaskState(
                    gen_id=gen_id,
                    status=status,
                    percent=100 if status == "succeeded" else 0,
                    stage="Done" if status == "succeeded" else "Failed",
                    error=error,
                )
            )
        )
        await websocket.close()
        return
    async for state in tasks.watch(gen_id):
        await websocket.send_json(asdict(state))


# ---- Optional static frontend (single-container / Docker deployments) ----
# When a web build exists (web/dist, or TEXDECK_WEB_DIST_DIR), serve the SPA
# from the same origin. Registered last, so /api, /ws and /health always win;
# unknown paths fall back to index.html for client-side routes like /g/<id>.
if settings.web_dist_dir.is_dir():
    _web_root = settings.web_dist_dir.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        candidate = (_web_root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(_web_root):
            return FileResponse(candidate)
        return FileResponse(_web_root / "index.html")
