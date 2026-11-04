"""Real tests for deck template loading, extract_tex, and message building."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.deck import (
    CompileError,
    DeckError,
    DeckRequest,
    _MAX_REPAIR_CALLS,
    _tex_context_around_error,
    build_generation_messages,
    compile_tex,
    copy_template_bundle,
    extract_tex,
    get_template,
    list_templates,
)
from app.edit import parse_frames
from app.pdf import PdfDocument, PdfPage


# Minimal complete Beamer document that satisfies every extract_tex check.
_MINIMAL_OK = r"""\documentclass[aspectratio=169,10pt]{beamer}
\usepackage[zh,bjtu]{collegebeamer}
\begin{document}
\begin{frame}{Hello}
Body text.
\end{frame}
\end{document}
"""


def _strip_instruction_frames(tex: str) -> str:
    """Remove [STYLE REF] and AI GENERATION INSTRUCTIONS frames (model-normal output)."""
    frames = parse_frames(tex)
    drop: set[int] = set()
    for frame in frames:
        if "[STYLE REF]" in frame.tex or "AI GENERATION INSTRUCTIONS" in frame.tex:
            for n in range(frame.start_line, frame.end_line + 1):
                drop.add(n)
    lines = tex.splitlines()
    kept = [line for i, line in enumerate(lines, start=1) if i not in drop]
    result = "\n".join(kept)
    if tex.endswith("\n"):
        result += "\n"
    return result


def test_list_templates_reads_real_templates_dir() -> None:
    templates = list_templates()
    assert len(templates) >= 1
    keys = {t.key for t in templates}
    names = {t.name for t in templates}
    # Real templates ship with template.json + main.tex
    assert "navy" in keys
    assert "Navy" in names
    for t in templates:
        assert t.dir.is_dir()
        assert (t.dir / "main.tex").is_file()
        assert (t.dir / "template.json").is_file()
        assert r"\documentclass" in t.main_tex


def test_get_template_returns_known_template() -> None:
    t = get_template("navy")
    assert t.key == "navy"
    assert t.name == "Navy"
    assert t.main_tex == (t.dir / "main.tex").read_text(encoding="utf-8")


def test_get_template_unknown_key_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown template"):
        get_template("this-template-does-not-exist-xyz")


def test_copy_template_bundle_ships_every_local_sty(tmp_path: Path) -> None:
    """Regression: only collegebeamer.sty used to be copied, so SimplePlus /
    Execushares generations compiled without their theme files — Execushares
    failed outright and SimplePlus silently picked up TeX Live's different copy.
    """
    for template in list_templates():
        target = tmp_path / template.key
        copy_template_bundle(template, target)
        expected = {p.name for p in template.dir.glob("*.sty")}
        got = {p.name for p in target.glob("*.sty")}
        assert got == expected, f"template {template.key} lost sty files"


def test_extract_tex_missing_documentclass() -> None:
    bad = _MINIMAL_OK.replace(r"\documentclass[aspectratio=169,10pt]{beamer}", "")
    with pytest.raises(DeckError, match=r"missing \\documentclass"):
        extract_tex(bad, None)


def test_extract_tex_missing_begin_document() -> None:
    bad = _MINIMAL_OK.replace(r"\begin{document}", "")
    with pytest.raises(DeckError, match=r"missing \\begin\{document\}"):
        extract_tex(bad, None)


def test_extract_tex_missing_end_document() -> None:
    bad = _MINIMAL_OK.replace(r"\end{document}", "")
    with pytest.raises(DeckError, match=r"missing \\end\{document\}"):
        extract_tex(bad, None)


def test_extract_tex_content_after_end_document() -> None:
    bad = _MINIMAL_OK.rstrip() + "\nextra trailing text\n"
    with pytest.raises(DeckError, match=r"content after \\end\{document\}"):
        extract_tex(bad, None)


@pytest.mark.parametrize(
    ("template_key", "directive"),
    [("emerald", r"\usepackage[zh,bjtu]{collegebeamer}"), ("simpleplus", r"\usetheme{SimplePlus}")],
)
def test_extract_tex_requires_the_selected_templates_theme(template_key: str, directive: str) -> None:
    """Each template declares its own theme; dropping it is not a valid deck.

    Regression guard: this check used to be hardcoded to collegebeamer, which made
    the \\usetheme-based templates impossible to generate.
    """
    template = get_template(template_key)
    ok = _MINIMAL_OK.replace(r"\usepackage[zh,bjtu]{collegebeamer}", directive)
    assert extract_tex(ok, template)

    bad = ok.replace(directive, r"\usepackage{graphicx}")
    with pytest.raises(DeckError, match="missing"):
        extract_tex(bad, template)


def test_extract_tex_leftover_style_ref_page() -> None:
    bad = _MINIMAL_OK.replace(
        r"\begin{frame}{Hello}" "\n" "Body text." "\n" r"\end{frame}",
        r"\begin{frame}{[STYLE REF] sample}" "\n" "x" "\n" r"\end{frame}",
    )
    with pytest.raises(DeckError, match=r"\[STYLE REF\]"):
        extract_tex(bad, None)


def test_extract_tex_leftover_ai_generation_instructions_page() -> None:
    bad = _MINIMAL_OK.replace(
        r"\begin{frame}{Hello}" "\n" "Body text." "\n" r"\end{frame}",
        r"\begin{frame}{AI GENERATION INSTRUCTIONS}" "\n" "x" "\n" r"\end{frame}",
    )
    with pytest.raises(DeckError, match="AI GENERATION INSTRUCTIONS"):
        extract_tex(bad, None)


def test_extract_tex_rejects_includegraphics() -> None:
    bad = _MINIMAL_OK.replace(
        "Body text.",
        r"\includegraphics{foo.png}",
    )
    with pytest.raises(DeckError, match=r"includegraphics"):
        extract_tex(bad, None)


def test_extract_tex_accepts_normal_document() -> None:
    out = extract_tex(_MINIMAL_OK, None)
    assert r"\documentclass" in out
    assert r"\end{document}" in out
    assert out.strip() == _MINIMAL_OK.strip()


def test_extract_tex_accepts_markdown_fences() -> None:
    fenced = "```latex\n" + _MINIMAL_OK + "\n```\n"
    out = extract_tex(fenced, None)
    assert r"\documentclass" in out
    assert "```" not in out


def test_extract_tex_passes_when_style_ref_only_in_header_comments() -> None:
    """Regression: top-of-file comments may contain '[STYLE REF]' text.

    After model-normal cleanup (sample STYLE REF / AI frames removed), extract_tex
    must still accept the document. Checking comments was a past production bug.
    """
    template = get_template("navy")
    assert "[STYLE REF]" in template.main_tex  # header comments keep the phrase

    cleaned = _strip_instruction_frames(template.main_tex)
    # Sample pages gone from body; phrase may still sit in % comments
    body_frames = parse_frames(cleaned)
    for frame in body_frames:
        assert "[STYLE REF]" not in frame.tex
        assert "AI GENERATION INSTRUCTIONS" not in frame.tex
    assert "[STYLE REF]" in cleaned  # still present in comment block

    out = extract_tex(cleaned, template)
    assert r"\begin{document}" in out
    assert r"\end{document}" in out


def test_build_generation_messages_order_and_verbatim_template() -> None:
    template = get_template("navy")
    request = DeckRequest(
        template_key=template.key,
        title="Quarterly Review",
        author="Jane Doe",
        institute="Dept. of Physics, MIT",
        instruction="Focus on risks",
        source_text="Paragraph of research notes for the deck.",
    )
    messages = build_generation_messages(template, request, documents=[])

    assert len(messages) == 5
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[2]["role"] == "user"
    assert messages[3]["role"] == "user"
    assert messages[4]["role"] == "user"

    # Core design: full template main.tex is injected verbatim as message #2
    assert messages[1]["content"] == template.main_tex
    assert messages[1]["content"] == (template.dir / "main.tex").read_text(encoding="utf-8")

    title_msg = messages[2]["content"]
    assert "Title: Quarterly Review" in title_msg
    assert "Author: Jane Doe" in title_msg
    assert "Institute: Dept. of Physics, MIT" in title_msg
    assert "Requirements: Focus on risks" in title_msg
    # English is the default output language; only Requirements may override it.
    assert "Output language: English" in title_msg
    assert "No author was provided" not in title_msg
    assert "No institute was provided" not in title_msg
    assert messages[4]["content"] == title_msg  # title requirement repeated
    assert messages[3]["content"] == request.source_text


def test_build_generation_messages_empty_author_and_institute() -> None:
    template = get_template("navy")
    request = DeckRequest(
        template_key=template.key,
        title="No Author Deck",
        author="",
        institute="",
        instruction="",
        source_text="Some body text for the slides.",
    )
    messages = build_generation_messages(template, request, documents=[])

    assert len(messages) == 5
    title_msg = messages[2]["content"]
    assert "Title: No Author Deck" in title_msg
    assert (
        "No author was provided; leave the author field empty rather than inventing one."
        in title_msg
    )
    assert (
        "No institute was provided; leave the institute field empty rather than inventing one."
        in title_msg
    )
    assert "Author:" not in title_msg
    assert "Institute:" not in title_msg
    assert "Requirements:" not in title_msg
    assert messages[4]["content"] == title_msg


def test_build_generation_messages_author_only() -> None:
    template = get_template("navy")
    request = DeckRequest(
        template_key=template.key,
        title="Author Only",
        author="Alice",
        institute="",
        instruction="Keep short",
        source_text="Body notes.",
    )
    messages = build_generation_messages(template, request, documents=[])
    title_msg = messages[2]["content"]
    assert "Author: Alice" in title_msg
    assert "No author was provided" not in title_msg
    assert (
        "No institute was provided; leave the institute field empty rather than inventing one."
        in title_msg
    )
    assert "Requirements: Keep short" in title_msg
    assert messages[4]["content"] == title_msg


def test_build_generation_messages_no_material_raises() -> None:
    template = get_template("navy")
    request = DeckRequest(
        template_key=template.key,
        title="Empty",
        instruction="",
        source_text="   \n  ",
    )
    with pytest.raises(DeckError, match="No source material"):
        build_generation_messages(template, request, documents=[])


def _fake_document(
    tmp_path: Path,
    *,
    index: int,
    name: str,
    n_pages: int,
    selected: list[int] | None = None,
) -> PdfDocument:
    """PdfDocument over stub PNG files (image_block only needs an existing file)."""
    doc_dir = tmp_path / f"doc_{index:02d}"
    doc_dir.mkdir(parents=True, exist_ok=True)
    pages: list[PdfPage] = []
    for n in range(1, n_pages + 1):
        png = doc_dir / f"page_{n:03d}.png"
        png.write_bytes(b"\x89PNG-stub")
        pages.append(PdfPage(number=n, image_path=png, text=f"text {n}"))
    return PdfDocument(
        path=tmp_path / name,
        name=name,
        index=index,
        pages=pages,
        selected_numbers=selected if selected is not None else list(range(1, n_pages + 1)),
    )


def _texts(material: list[dict[str, Any]]) -> list[str]:
    return [part["text"] for part in material if part.get("type") == "text"]


def test_build_generation_messages_multi_doc_boundaries_and_labels(tmp_path: Path) -> None:
    template = get_template("navy")
    request = DeckRequest(template_key=template.key, title="Multi Doc Deck")
    doc_a = _fake_document(tmp_path, index=1, name="a.pdf", n_pages=2)
    doc_b = _fake_document(tmp_path, index=2, name="b.pdf", n_pages=1)

    messages = build_generation_messages(template, request, documents=[doc_a, doc_b])

    # Envelope untouched: 5 segments, template verbatim in segment 2
    assert len(messages) == 5
    assert messages[1]["content"] == template.main_tex
    assert messages[4]["content"] == messages[2]["content"]

    material = messages[3]["content"]
    assert isinstance(material, list)
    texts = _texts(material)
    assert texts[0] == (
        "Source material consists of 2 documents, in reading order. "
        "When citing, use the document name and its own page number."
    )
    assert '--- Document 1 of 2: "a.pdf" (2 pages, 2 shown) ---' in texts
    assert '--- Document 2 of 2: "b.pdf" (1 pages, 1 shown) ---' in texts
    assert 'Doc 1 ("a.pdf") page 1' in texts
    assert 'Doc 1 ("a.pdf") page 2' in texts
    assert 'Doc 2 ("b.pdf") page 1' in texts
    # Boundary blocks precede their document's pages
    assert texts.index('--- Document 1 of 2: "a.pdf" (2 pages, 2 shown) ---') < texts.index(
        'Doc 1 ("a.pdf") page 1'
    )
    assert texts.index('--- Document 2 of 2: "b.pdf" (1 pages, 1 shown) ---') > texts.index(
        'Doc 1 ("a.pdf") page 2'
    )
    # One image per selected page, each immediately followed by its label
    images = [i for i, part in enumerate(material) if part.get("type") == "image_url"]
    assert len(images) == 3
    for i in images:
        assert material[i + 1]["type"] == "text"


def test_build_generation_messages_single_doc_stays_identical(tmp_path: Path) -> None:
    """One document must produce today's exact material: no header, no boundary
    blocks, labels exactly 'Page N' (locks in the no-drift guarantee)."""
    template = get_template("navy")
    request = DeckRequest(template_key=template.key, title="Single Doc Deck")
    document = _fake_document(tmp_path, index=1, name="a.pdf", n_pages=3, selected=[1, 3])

    messages = build_generation_messages(template, request, documents=[document])

    assert len(messages) == 5
    material = messages[3]["content"]
    assert isinstance(material, list)
    # Exactly image+label per selected page and nothing else
    assert [part.get("type") for part in material] == ["image_url", "text", "image_url", "text"]
    assert _texts(material) == ["Page 1", "Page 3"]
    for text in _texts(material):
        assert "Doc" not in text and "Document" not in text


def test_build_generation_messages_doc_without_selected_pages_raises(tmp_path: Path) -> None:
    template = get_template("navy")
    request = DeckRequest(template_key=template.key, title="No Pages")
    document = _fake_document(tmp_path, index=1, name="a.pdf", n_pages=2, selected=[])
    with pytest.raises(DeckError, match="No source material"):
        build_generation_messages(template, request, documents=[document])


def test_tex_context_around_error_returns_whole_frame() -> None:
    """Alignment errors often report \\end{frame}; context must still include the table."""
    tex = r"""\documentclass{beamer}
\begin{document}
\begin{frame}{Before}
Normal page.
\end{frame}
\begin{frame}{Bad table}
\begin{tabular}{ccc}
a & b & c \\
d & e & f & EXTRA \\
\end{tabular}
\end{frame}
\begin{frame}{After}
Also fine.
\end{frame}
\end{document}
"""
    # Simulate XeLaTeX pointing at \end{frame} (line of the bad frame's end).
    # First \end{frame} is the "Before" page; second is the bad table page.
    lines = tex.splitlines()
    end_frames = [i for i, line in enumerate(lines, start=1) if r"\end{frame}" in line]
    bad_end = end_frames[1]
    compile_log = f"! Extra alignment tab has been changed to \\cr\nl.{bad_end}\n"
    ctx = _tex_context_around_error(tex, compile_log)

    assert r"\begin{frame}{Bad table}" in ctx
    assert r"\begin{tabular}{ccc}" in ctx
    assert "EXTRA" in ctx
    assert r"\end{frame}" in ctx
    assert f">>> {bad_end}:" in ctx
    # Must not be just a tiny window missing the table body.
    assert "d & e & f & EXTRA" in ctx
    # Surrounding frames should not be included.
    assert "Before" not in ctx
    assert "After" not in ctx


def test_tex_context_around_error_preamble_fallback_uses_radius() -> None:
    tex = "\n".join(f"line {i}" for i in range(1, 31))
    # Inject a fake preamble-ish error on line 5 (no frame wrappers).
    compile_log = "! Undefined control sequence.\nl.5\n"
    ctx = _tex_context_around_error(tex, compile_log, radius=10)
    assert ">>> 5: line 5" in ctx
    assert "line 1" in ctx  # 5-10 would be <1 so starts at 1? max(1,5-10)=1
    assert "line 15" in ctx  # 5+10
    assert "line 16" not in ctx


def test_tex_context_around_error_caps_large_frame() -> None:
    body = "\n".join(f"row {i} \\\\" for i in range(1, 220))
    tex = (
        "\\documentclass{beamer}\n"
        "\\begin{document}\n"
        "\\begin{frame}{Huge}\n"
        f"{body}\n"
        "\\end{frame}\n"
        "\\end{document}\n"
    )
    lines = tex.splitlines()
    # Error reported near the end of the huge frame.
    end_line = next(i for i, line in enumerate(lines, start=1) if r"\end{frame}" in line)
    error_line = end_line - 5
    compile_log = f"! Something wrong.\nl.{error_line}\n"
    ctx = _tex_context_around_error(tex, compile_log)
    ctx_lines = ctx.splitlines()
    # Cap: at most 60 before + error + 20 after = 81 lines (may be less near edges).
    assert len(ctx_lines) <= 81
    assert f">>> {error_line}:" in ctx
    # Full frame would be >200; must not dump every row.
    assert len(ctx_lines) < 200


class _FakeRepairClient:
    """Returns a fixed repaired tex on complete(); records calls."""

    def __init__(self, repaired: str) -> None:
        self.repaired = repaired
        self.calls: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]], **_kwargs: Any) -> str:
        self.calls.append(messages)
        return self.repaired


class _SequencedRepairClient:
    """Fake client: returns contents in order, one per complete() call."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]], **_kwargs: Any) -> str:
        self.calls.append(messages)
        if len(self.calls) > len(self._responses):
            raise AssertionError(
                f"complete() called {len(self.calls)} times; "
                f"only {len(self._responses)} responses prepared"
            )
        return self._responses[len(self.calls) - 1]


def _bad_tabular_tex() -> str:
    return _MINIMAL_OK.replace(
        "Body text.",
        r"\begin{tabular}{cc}" "\n" r"a & b & EXTRA \\" "\n" r"\end{tabular}",
    )


def _fake_xelatex_fail_until_fixed(state: dict[str, int]):
    """Fail while source still has EXTRA; succeed (and write PDF) once fixed."""

    def fake_run(path: Path, work_dir: Path) -> tuple[bool, str]:
        state["n"] = state.get("n", 0) + 1
        src = path.read_text(encoding="utf-8")
        if "EXTRA" in src:
            lines = src.splitlines()
            end_ln = next(
                i for i, ln in enumerate(lines, start=1) if r"\end{frame}" in ln
            )
            log = f"! Extra alignment tab has been changed to \\cr\nl.{end_ln}\n"
            return False, log
        (work_dir / f"{path.stem}.pdf").write_bytes(b"%PDF-fake")
        return True, "ok"

    return fake_run


def test_compile_tex_dumps_repair_prompt_and_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When llm_dump_dir is set, each repair round writes prompt/response files."""
    good = _MINIMAL_OK
    bad = _bad_tabular_tex()
    tex_path = tmp_path / "main.tex"
    tex_path.write_text(bad, encoding="utf-8")
    dump_dir = tmp_path / "llm"

    # Avoid real xelatex: first call fails, second (after repair write) succeeds.
    state = {"n": 0}

    def fake_run(path: Path, work_dir: Path) -> tuple[bool, str]:
        state["n"] += 1
        if state["n"] == 1:
            # Point at \end{frame} like real alignment errors.
            src = path.read_text(encoding="utf-8")
            lines = src.splitlines()
            end_ln = next(i for i, ln in enumerate(lines, start=1) if r"\end{frame}" in ln)
            log = f"! Extra alignment tab has been changed to \\cr\nl.{end_ln}\n"
            return False, log
        # After repair, two successful passes.
        (work_dir / f"{path.stem}.pdf").write_bytes(b"%PDF-fake")
        return True, "ok"

    monkeypatch.setattr("app.deck._run_xelatex", fake_run)
    monkeypatch.setattr("app.deck.settings.max_compile_repairs", 2)
    # which(xelatex) check
    monkeypatch.setattr("app.deck.shutil.which", lambda _cmd: "/usr/bin/xelatex")

    client = _FakeRepairClient(good)
    result = compile_tex(tex_path, client=client, llm_dump_dir=dump_dir)

    assert result.pdf_path.is_file()
    prompt = dump_dir / "repair_1_prompt.json"
    response = dump_dir / "repair_1_response.txt"
    assert prompt.is_file()
    assert response.is_file()
    prompt_text = prompt.read_text(encoding="utf-8")
    assert "Extra alignment tab" in prompt_text
    assert "tabular" in prompt_text or "EXTRA" in prompt_text
    assert response.read_text(encoding="utf-8") == good
    assert len(client.calls) == 1


def test_compile_tex_noop_repair_does_not_consume_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model echo (noop) must not consume max_compile_repairs.

    Sequence: 1st call returns identical bad tex; 2nd returns the fix.
    With max_compile_repairs=1 this must still succeed — proving the noop
    did not burn the only effective repair slot.
    """
    bad = _bad_tabular_tex()
    good = _MINIMAL_OK
    tex_path = tmp_path / "main.tex"
    tex_path.write_text(bad, encoding="utf-8")
    dump_dir = tmp_path / "llm"

    monkeypatch.setattr(
        "app.deck._run_xelatex", _fake_xelatex_fail_until_fixed({"n": 0})
    )
    monkeypatch.setattr("app.deck.settings.max_compile_repairs", 1)
    monkeypatch.setattr("app.deck.shutil.which", lambda _cmd: "/usr/bin/xelatex")

    client = _SequencedRepairClient([bad, good])
    result = compile_tex(tex_path, client=client, llm_dump_dir=dump_dir)

    assert result.pdf_path.is_file()
    assert len(client.calls) == 2
    # Noop dump uses the call index and _noop stem.
    assert (dump_dir / "repair_1_noop_response.txt").is_file()
    assert (dump_dir / "repair_1_noop_prompt.json").is_file()
    assert (dump_dir / "repair_2_response.txt").is_file()
    assert (dump_dir / "repair_2_prompt.json").is_file()
    assert "EXTRA" not in tex_path.read_text(encoding="utf-8")


def test_compile_tex_all_noop_hits_max_repair_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All-echo sequence must stop at _MAX_REPAIR_CALLS, not loop forever."""
    bad = _bad_tabular_tex()
    tex_path = tmp_path / "main.tex"
    tex_path.write_text(bad, encoding="utf-8")
    dump_dir = tmp_path / "llm"

    monkeypatch.setattr(
        "app.deck._run_xelatex", _fake_xelatex_fail_until_fixed({"n": 0})
    )
    monkeypatch.setattr("app.deck.settings.max_compile_repairs", 2)
    monkeypatch.setattr("app.deck.shutil.which", lambda _cmd: "/usr/bin/xelatex")

    # More noops than the hard call cap — client should be invoked exactly once
    # per call up to _MAX_REPAIR_CALLS, then CompileError.
    client = _SequencedRepairClient([bad] * (_MAX_REPAIR_CALLS + 5))

    with pytest.raises(CompileError, match="LaTeX compilation failed"):
        compile_tex(tex_path, client=client, llm_dump_dir=dump_dir)

    assert len(client.calls) == _MAX_REPAIR_CALLS
    for i in range(1, _MAX_REPAIR_CALLS + 1):
        assert (dump_dir / f"repair_{i}_noop_response.txt").is_file()
    # File must remain the unbroken original (no write-back on noop).
    assert tex_path.read_text(encoding="utf-8") == bad


def test_compile_tex_no_client_raises_without_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tex_path = tmp_path / "main.tex"
    tex_path.write_text(_MINIMAL_OK, encoding="utf-8")
    dump_dir = tmp_path / "llm"

    def fake_run(path: Path, work_dir: Path) -> tuple[bool, str]:
        return False, "! Undefined control sequence.\nl.1\n"

    monkeypatch.setattr("app.deck._run_xelatex", fake_run)
    monkeypatch.setattr("app.deck.shutil.which", lambda _cmd: "/usr/bin/xelatex")

    with pytest.raises(CompileError, match="LaTeX compilation failed"):
        compile_tex(tex_path, client=None, llm_dump_dir=dump_dir)

    assert not dump_dir.exists() or not any(dump_dir.iterdir())


def test_tex_engine_auto_prefers_xelatex_then_tectonic(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings
    from app.deck import CompileError, tex_engine

    found: set[str] = {"xelatex", "tectonic"}
    monkeypatch.setattr("app.deck.shutil.which", lambda cmd: f"/bin/{cmd}" if cmd in found else None)
    monkeypatch.setattr(settings, "tex_engine", "auto")
    assert tex_engine() == "xelatex"
    found.discard("xelatex")
    assert tex_engine() == "tectonic"
    found.clear()
    with pytest.raises(CompileError, match="No TeX engine"):
        tex_engine()
    monkeypatch.setattr(settings, "tex_engine", "tectonic")
    with pytest.raises(CompileError, match="tectonic not found"):
        tex_engine()
