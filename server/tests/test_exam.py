"""Learn: exams (profile, papers, question checks, typesetting, API, migration)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import exam, store
from app.main import _run_exam_paper, _run_exam_profile, app
from tests.conftest import FakeLlm


def _spec() -> dict[str, Any]:
    return {"types": [
        {"type": "single", "count": 2, "points": 2},
        {"type": "fill", "count": 1, "points": 3},
        {"type": "calc", "count": 0, "points": 0, "items": [{"points": 10, "parts": "4+6"}]},
        {"type": "truefalse", "count": 0, "points": 1},
    ], "duration": 60, "description": "Midterm, closed book", "extra": "Focus on chapter 2"}


def _profile() -> dict[str, Any]:
    return {
        "title": "Deep learning midterm", "instructions": "", "duration": 60, "style": "short stems",
        "marking": "Steps earn points.",
        "sections": [
            {"title": "一、单项选择题（共 2 题，每题 2 分）", "type": "single", "count": 2, "points": 2},
            {"title": "二、填空题", "type": "fill", "count": 1, "points": [3]},
            {"title": "三、计算题", "type": "calc", "count": 1, "points": [10], "parts": ["4+6"]},
        ],
        "topics": [{"name": "Residual learning", "weight": 60, "note": ""}, {"name": "Depth", "weight": 40}],
    }


def _paper() -> dict[str, Any]:
    return {"title": "Residual nets quiz", "instructions": "Answer all.", "sections": [
        {"title": "x", "questions": [
            {"stem": "What does a shortcut add?", "options": ["A. Identity", "B. Noise", "C. Depth", "D. Loss"],
             "answer": "A", "explanation": "(p. 2)", "topic": "Residual learning"},
            {"stem": "Deeper plain nets have higher $\\epsilon_{train}$ because?", "options": ["x", "y", "z", "w"],
             "answer": 2, "topic": "Depth"},
        ]},
        {"title": "y", "questions": [
            {"stem": "ResNet-{{1}} has {{2}} layers", "answer": [["152", "一五二"], ["152"]]},
        ]},
        {"title": "z", "questions": [
            {"stem": "Compute the output.", "answer": "y = F(x) + x",
             "parts": [{"stem": "(1) F", "answer": "a"}, {"stem": "(2) y", "answer": "b"}],
             "rubric": ["identity (4 points)"]},
        ]},
    ]}


def test_json_with_undoubled_latex_backslashes_still_parses() -> None:
    reply = r'{"score": 9.5, "feedback": "Gave $y = F(x, \{W_i\}) + x$ and \alpha.\nMissing the 1x1 layers."}'
    data = exam.parse_json(reply)
    assert data["feedback"] == "Gave $y = F(x, \\{W_i\\}) + x$ and \\alpha.\nMissing the 1x1 layers."
    assert exam.parse_json(r'{"a": "\\beta \"q\" \u00e9"}') == {"a": '\\beta "q" é'}


def test_spec_drops_empty_rows_and_keeps_the_description() -> None:
    spec = exam.clean_spec(_spec())
    assert [(r["type"], r["count"]) for r in spec["types"]] == [("single", 2), ("fill", 1), ("calc", 1)]
    assert spec["duration"] == 60 and spec["description"] == "Midterm, closed book"


def test_everything_in_the_spec_is_optional_but_other_needs_a_description() -> None:
    assert exam.clean_spec({})["types"] == []
    with pytest.raises(exam.ExamError, match="Other"):
        exam.clean_spec({"types": [{"type": "other", "count": 2, "points": 5}]})


def test_profile_keeps_topics_and_marking_and_drops_heading_tallies() -> None:
    p = exam.clean_profile(_profile())
    assert [s["title"] for s in p["sections"]] == ["一、单项选择题", "二、填空题", "三、计算题"]
    assert p["sections"][0]["points"] == [2, 2] and p["sections"][2]["parts"] == ["4+6"]
    assert [t["name"] for t in p["topics"]] == ["Residual learning", "Depth"] and p["marking"] == "Steps earn points."
    assert exam.clean_profile(p) == p


def test_headings_lose_the_tally_the_paper_adds_itself() -> None:
    p = exam.clean_profile({"sections": [
        {"title": "Part B (5 questions, 10 points)", "type": "fill", "count": 5, "points": 2},
        {"title": "三、简答题（Shortcut）", "type": "short", "count": 1, "points": [6]},
    ]})
    assert [s["title"] for s in p["sections"]] == ["Part B", "三、简答题（Shortcut）"]


def test_paper_is_checked_against_the_profile_and_scored_from_it() -> None:
    paper = exam.clean_exam(_paper(), exam.clean_profile(_profile()))
    qs = [q for s in paper["sections"] for q in s["questions"]]
    assert [q["id"] for q in qs] == ["q1", "q2", "q3", "q4"]
    assert qs[0]["options"][0] == "Identity" and qs[0]["answer"] == "A" and qs[0]["topic"] == "Residual learning"
    assert qs[1]["answer"] == "C"
    assert qs[2]["mode"] == "fill" and qs[2]["answer"] == [["152", "一五二"], ["152"]]
    assert [p["points"] for p in qs[3]["parts"]] == [4, 6]
    assert paper["total"] == 2 + 2 + 3 + 10


def test_a_short_section_is_an_error_for_the_retry() -> None:
    bad = _paper()
    bad["sections"][0]["questions"].pop()
    with pytest.raises(exam.ExamError, match="has 1 questions"):
        exam.clean_exam(bad, exam.clean_profile(_profile()))


def test_fill_answers_must_match_the_blanks() -> None:
    with pytest.raises(exam.ExamError, match="blank"):
        exam.clean_question({"stem": "{{1}} and {{2}}", "answer": ["a"]}, "fill", 2, "")
    q = exam.clean_question({"stem": "The answer is", "answer": "42"}, "fill", 2, "")
    assert q["stem"].endswith("{{1}}") and q["answer"] == [["42"]]


def test_tex_escapes_text_keeps_maths_and_refuses_unsafe_maths() -> None:
    assert exam.to_tex("50% of $x^2$ & more") == r"50\% of $x^2$ \& more"
    assert exam.to_tex(r"see \(a_1\)") == "see $a_1$"
    assert "$\\input" not in exam.to_tex(r"$\input{x}$")
    assert exam.to_tex("$a{b$") == r"\$a\{b\$"
    assert exam.to_tex("fill {{1}}", blank="___") == "fill ___"


def test_render_paper_and_key() -> None:
    paper = exam.clean_exam(_paper(), exam.clean_profile(_profile()))
    tex = exam.render(paper, "zh", answers=False)
    assert "一、单项选择题（共 2 题，每题 2 分，共 4 分）" in tex
    assert "Identity" in tex and "y = F(x) + x" not in tex
    key = exam.render(paper, "zh", answers=True)
    assert "参考答案" in key and "y = F(x) + x" in key


def test_past_papers_go_as_text_when_they_have_a_text_layer(tmp_path: Path) -> None:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(72, 72, 540, 700), "1. Which layer adds the identity? " * 12, fontsize=11)
    exam.store_past(tmp_path, [("2023.pdf", doc.tobytes())])
    blocks = exam.past_blocks(tmp_path)
    assert blocks[0]["text"] == "--- Past paper 1 ---"
    assert "identity" in blocks[1]["text"] and blocks[2]["type"] != "text"  # text, then page 1 for the layout


# ---- API ---------------------------------------------------------------------------


@pytest.fixture
def client(artifact_root: Path) -> TestClient:
    with TestClient(app) as c:
        yield c


def _create(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr("app.main.tasks.start", lambda gid, job: None)  # the job is run by hand below
    r = client.post("/api/exams", data={"title": "", "language": "en", "spec": json.dumps(_spec()),
                                        "source_text": "Residual learning reformulates layers."})
    assert r.status_code == 202, r.text
    gid = r.json()["id"]
    fake = FakeLlm([json.dumps(_profile())])
    monkeypatch.setattr("app.main.create_client", lambda on_usage=None: fake)
    _run_exam_profile(gid, lambda _s, _p: None)
    material = fake.calls[0][1]["content"]
    assert any("Midterm, closed book" in b.get("text", "") for b in material)  # the description reaches the model
    return gid


def _write_paper(gid: str, monkeypatch: pytest.MonkeyPatch, *replies: str) -> FakeLlm:
    fake = FakeLlm(list(replies))
    monkeypatch.setattr("app.main.create_client", lambda on_usage=None: fake)
    _run_exam_paper(gid, "", lambda _s, _p: None)
    return fake


def test_the_profile_is_drafted_then_edited(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    gid = _create(client, monkeypatch)
    state = client.get(f"/api/generations/{gid}/exam").json()
    assert state["stage"] == "profile" and len(state["profile"]["sections"]) == 3
    meta = store.load_meta(gid)
    assert meta.status == "review" and meta.title == "Deep learning midterm"
    edited = {**state["profile"], "title": "Edited"}
    assert client.put(f"/api/generations/{gid}/exam/profile", json={"profile": edited}).json()["title"] == "Edited"
    assert client.post(f"/api/generations/{gid}/regenerate").status_code == 400


def test_papers_pile_up_and_later_ones_see_the_earlier_questions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_xelatex_ok: dict[str, int],
) -> None:
    gid = _create(client, monkeypatch)
    bad = _paper()
    bad["sections"][1]["questions"] = []
    fake = _write_paper(gid, monkeypatch, json.dumps(bad), json.dumps(_paper()))
    assert "has 0 questions" in fake.calls[1][-1]["content"]  # the retry says what was wrong
    fake = _write_paper(gid, monkeypatch, json.dumps(_paper()))
    earlier = next(b["text"] for b in fake.calls[0][1]["content"] if "earlier papers" in b.get("text", ""))
    assert "What does a shortcut add?" in earlier
    state = client.get(f"/api/generations/{gid}/exam").json()
    assert state["stage"] == "ready" and [p["id"] for p in state["papers"]] == ["001", "002"]
    assert client.get(f"/api/generations/{gid}/exam/papers/002/paper.pdf").status_code == 200
    assert client.get(f"/api/generations/{gid}/exam/papers/002").json()["sections"][0]["questions"][0]["topic"]
    assert client.delete(f"/api/generations/{gid}/exam/papers/001").status_code == 204
    assert [p["id"] for p in client.get(f"/api/generations/{gid}/exam").json()["papers"]] == ["002"]
    assert client.get(f"/api/generations/{gid}/exam/papers/1x").status_code == 404
    assert client.get(f"/api/generations/{gid}/exam/papers/%2E%2E/paper.pdf").status_code == 404


def test_a_failed_paper_leaves_nothing_behind(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    gid = _create(client, monkeypatch)
    with pytest.raises(exam.ExamError):
        _write_paper(gid, monkeypatch, "not json", "still not json")
    assert client.get(f"/api/generations/{gid}/exam").json()["papers"] == []
    assert store.load_meta(gid).status == "failed"


def test_open_answers_are_graded_with_the_exams_marking(client: TestClient, monkeypatch: pytest.MonkeyPatch,
                                                        fake_xelatex_ok: dict[str, int]) -> None:
    gid = _create(client, monkeypatch)
    _write_paper(gid, monkeypatch, json.dumps(_paper()))

    class Now:
        def submit(self, fn: Any, *args: Any) -> None:
            fn(*args)

    grader = FakeLlm(['{"score": 99, "feedback": "Good."}'])
    monkeypatch.setattr(exam, "_pool", Now())
    monkeypatch.setattr(exam, "create_client", lambda: grader)
    r = client.post(f"/api/generations/{gid}/exam/papers/001/grade", data={"qid": "q4", "part": "1", "answer": "y=F+x"})
    assert r.status_code == 202, r.text
    grade = client.get(f"/api/generations/{gid}/exam/grade/{r.json()['id']}").json()
    assert grade["status"] == "done" and grade["score"] == 6 and grade["max"] == 6  # clamped to the part's points
    assert "Steps earn points." in grader.calls[0][1]["content"][0]["text"]
    assert client.post(f"/api/generations/{gid}/exam/papers/001/grade", data={"qid": "q1", "answer": "A"}).status_code == 400
    attempts = client.post(f"/api/generations/{gid}/exam/papers/001/attempts",
                           json={"score": 10, "total": 17, "answered": 4, "correct": 2}).json()
    assert attempts[0]["score"] == 10


def test_an_exam_from_before_papers_is_migrated(client: TestClient, monkeypatch: pytest.MonkeyPatch,
                                                artifact_root: Path) -> None:
    gid = _create(client, monkeypatch)
    d = artifact_root / gid / "exam"
    (d / "profile.json").rename(d / "blueprint.json")  # the old layout
    (d / "exam.json").write_text(json.dumps(exam.clean_exam(_paper(), exam.clean_profile(_profile()))), encoding="utf-8")
    (d / "paper.pdf").write_bytes(b"%PDF-old")
    (d / "attempts.json").write_text("[]", encoding="utf-8")
    (d / "samples").mkdir()
    (d / "samples" / "sample-01.png").write_bytes(b"png")
    state = client.get(f"/api/generations/{gid}/exam").json()
    assert state["profile"]["topics"] and [p["id"] for p in state["papers"]] == ["001"]
    assert state["past"] == ["past-01.png"]
    assert (d / "papers" / "001" / "paper.pdf").read_bytes() == b"%PDF-old" and not (d / "blueprint.json").exists()


# ---- results, mastery, drills -------------------------------------------------------------


def test_results_build_a_recency_weighted_mastery_and_reach_the_next_paper(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_xelatex_ok: dict[str, int],
) -> None:
    gid = _create(client, monkeypatch)
    _write_paper(gid, monkeypatch, json.dumps(_paper()))
    post = lambda qid, score: client.post(f"/api/generations/{gid}/exam/papers/001/results", json={"qid": qid, "score": score})  # noqa: E731
    assert post("q1", 0).json()["topic"] == "Residual learning"
    post("q1", 2)  # wrong first, right later: the later answer counts more
    post("q2", 0)
    assert post("q9", 1).status_code == 400
    rows = {m["name"]: m for m in client.get(f"/api/generations/{gid}/exam").json()["mastery"]}
    assert rows["Residual learning"]["answered"] == 2
    assert rows["Residual learning"]["mastery"] == pytest.approx(1 / 1.75, abs=0.001)
    assert rows["Depth"]["mastery"] == 0 and rows["Depth"]["weight"] == 40
    fake = _write_paper(gid, monkeypatch, json.dumps(_paper()))
    standing = next(b["text"] for b in fake.calls[0][1]["content"] if "results so far" in b.get("text", ""))
    assert standing.index("Depth: 0%") < standing.index("Residual learning: 57%")  # weakest first


def test_a_drill_is_one_type_on_one_topic(client: TestClient, monkeypatch: pytest.MonkeyPatch,
                                          fake_xelatex_ok: dict[str, int]) -> None:
    gid = _create(client, monkeypatch)
    assert client.post(f"/api/generations/{gid}/exam/drills", json={"topic": "Depth", "type": "calc", "count": 3}).status_code == 400
    questions = [{"stem": f"Step {i}: compute.", "answer": "x", "topic": "Depth"} for i in range(6)]
    fake = FakeLlm([json.dumps({"title": "Depth", "sections": [{"title": "s", "questions": questions}]})])
    monkeypatch.setattr("app.main.create_client", lambda on_usage=None: fake)
    _run_exam_paper(gid, "", lambda _s, _p: None, {"topic": "Depth", "type": "calc", "count": 6})
    assert "This paper is a drill" in fake.calls[0][0]["content"]
    profile_sent = next(b["text"] for b in fake.calls[0][1]["content"] if b.get("text", "").startswith("Exam profile"))
    assert '"count": 6' in profile_sent and '"name": "Depth"' in profile_sent and "Residual learning" not in profile_sent
    summary = client.get(f"/api/generations/{gid}/exam").json()["papers"][0]
    assert summary["drill"] == {"topic": "Depth", "type": "calc", "count": 6} and summary["questions"] == 6
    paper = client.get(f"/api/generations/{gid}/exam/papers/{summary['id']}").json()
    assert paper["total"] == 60  # the profile's calculation points, per question
