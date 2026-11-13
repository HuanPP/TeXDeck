"""Learn: practice exams (spec, blueprint, question checks, typesetting, API)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import exam, store
from app.main import _run_exam_paper, app
from tests.conftest import FakeLlm


def _spec() -> dict[str, Any]:
    return {"types": [
        {"type": "single", "count": 2, "points": 2},
        {"type": "fill", "count": 1, "points": 3},
        {"type": "calc", "count": 0, "points": 0, "items": [{"points": 10, "parts": "4+6"}]},
        {"type": "truefalse", "count": 0, "points": 1},
    ], "duration": 60, "extra": "Focus on chapter 2"}


def _paper() -> dict[str, Any]:
    return {"title": "Residual nets quiz", "instructions": "Answer all.", "sections": [
        {"title": "x", "questions": [
            {"stem": "What does a shortcut add?", "options": ["A. Identity", "B. Noise", "C. Depth", "D. Loss"],
             "answer": "A", "explanation": "(p. 2)"},
            {"stem": "Deeper plain nets have higher $\\epsilon_{train}$ because?", "options": ["x", "y", "z", "w"],
             "answer": 2},
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


def test_spec_drops_empty_rows_and_takes_counts_from_open_items() -> None:
    spec = exam.clean_spec(_spec(), samples=0)
    assert [(r["type"], r["count"]) for r in spec["types"]] == [("single", 2), ("fill", 1), ("calc", 1)]
    assert spec["duration"] == 60


def test_spec_needs_types_or_samples_and_a_description_for_other() -> None:
    with pytest.raises(exam.ExamError):
        exam.clean_spec({"types": []}, samples=0)
    assert exam.clean_spec({"types": []}, samples=1)["types"] == []
    with pytest.raises(exam.ExamError, match="Other"):
        exam.clean_spec({"types": [{"type": "other", "count": 2, "points": 5}]}, samples=0)


def test_local_blueprint_numbers_sections_in_the_paper_language() -> None:
    bp = exam.local_blueprint(exam.clean_spec(_spec(), 0), "Quiz", "zh")
    assert [s["title"] for s in bp["sections"]] == ["一、单项选择题", "二、填空题", "三、计算题"]
    assert bp["sections"][2]["points"] == [10] and bp["sections"][2]["parts"] == ["4+6"]
    assert exam.clean_blueprint(bp) == bp


def test_paper_is_checked_against_the_blueprint_and_scored_from_it() -> None:
    bp = exam.local_blueprint(exam.clean_spec(_spec(), 0), "Quiz", "en")
    paper = exam.clean_exam(_paper(), bp)
    qs = [q for s in paper["sections"] for q in s["questions"]]
    assert [q["id"] for q in qs] == ["q1", "q2", "q3", "q4"]
    assert qs[0]["options"][0] == "Identity" and qs[0]["answer"] == "A"
    assert qs[1]["answer"] == "C"
    assert qs[2]["mode"] == "fill" and qs[2]["answer"] == [["152", "一五二"], ["152"]]
    assert [p["points"] for p in qs[3]["parts"]] == [4, 6]
    assert paper["total"] == 2 + 2 + 3 + 10


def test_a_short_section_is_an_error_for_the_retry() -> None:
    bp = exam.local_blueprint(exam.clean_spec(_spec(), 0), "Quiz", "en")
    bad = _paper()
    bad["sections"][0]["questions"].pop()
    with pytest.raises(exam.ExamError, match="has 1 questions"):
        exam.clean_exam(bad, bp)


def test_fill_answers_must_match_the_blanks() -> None:
    with pytest.raises(exam.ExamError, match="blank"):
        exam.clean_question({"stem": "{{1}} and {{2}}", "answer": ["a"]}, "fill", 2, "")
    q = exam.clean_question({"stem": "The answer is", "answer": "42"}, "fill", 2, "")
    assert q["stem"].endswith("{{1}}") and q["answer"] == [["42"]]


def test_tex_escapes_text_keeps_maths_and_refuses_unsafe_maths() -> None:
    assert exam.to_tex("50% of $x^2$ & more") == r"50\% of $x^2$ \& more"
    assert exam.to_tex(r"see \(a_1\)") == "see $a_1$"
    assert "input" in exam.to_tex(r"$\input{/etc/passwd}$") and "$\\input" not in exam.to_tex(r"$\input{x}$")
    assert exam.to_tex("$a{b$") == r"\$a\{b\$"
    assert exam.to_tex("fill {{1}}", blank="___") == "fill ___"


def test_render_paper_and_key() -> None:
    bp = exam.local_blueprint(exam.clean_spec(_spec(), 0), "Quiz", "zh")
    paper = exam.clean_exam(_paper(), bp)
    tex = exam.render(paper, "zh", answers=False)
    assert "一、单项选择题（共 2 题，每题 2 分，共 4 分）" in tex
    assert "Identity" in tex and "y = F(x) + x" not in tex
    key = exam.render(paper, "zh", answers=True)
    assert "参考答案" in key and "y = F(x) + x" in key


# ---- API ---------------------------------------------------------------------------


@pytest.fixture
def client(artifact_root: Path) -> TestClient:
    with TestClient(app) as c:
        yield c


def _create(client: TestClient) -> str:
    r = client.post("/api/exams", data={"title": "Quiz", "language": "en", "spec": json.dumps(_spec()),
                                        "source_text": "Residual learning reformulates layers."})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def test_create_without_samples_goes_straight_to_the_blueprint(client: TestClient) -> None:
    gid = _create(client)
    state = client.get(f"/api/generations/{gid}/exam").json()
    assert state["stage"] == "blueprint" and len(state["blueprint"]["sections"]) == 3
    assert store.load_meta(gid).status == "review"
    edited = {**state["blueprint"], "title": "Edited"}
    assert client.put(f"/api/generations/{gid}/exam/blueprint", json={"blueprint": edited}).json()["title"] == "Edited"
    assert client.post(f"/api/generations/{gid}/regenerate").status_code == 400


def test_paper_job_writes_the_exam_and_typesets_both_pdfs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_xelatex_ok: dict[str, int],
) -> None:
    gid = _create(client)
    bad = _paper()
    bad["sections"][1]["questions"] = []
    fake = FakeLlm([json.dumps(bad), json.dumps(_paper())])
    monkeypatch.setattr("app.main.create_client", lambda on_usage=None: fake)
    _run_exam_paper(gid, lambda _s, _p: None)
    assert "has 0 questions" in fake.calls[1][-1]["content"]  # the retry says what was wrong
    state = client.get(f"/api/generations/{gid}/exam").json()
    assert state["stage"] == "ready" and state["paper"] and state["answers"]
    assert store.load_meta(gid).status == "succeeded"
    assert client.get(f"/api/generations/{gid}/exam/paper.pdf").status_code == 200


def test_open_answers_are_graded_by_the_model(client: TestClient, monkeypatch: pytest.MonkeyPatch,
                                               fake_xelatex_ok: dict[str, int]) -> None:
    gid = _create(client)
    monkeypatch.setattr("app.main.create_client", lambda on_usage=None: FakeLlm([json.dumps(_paper())]))
    _run_exam_paper(gid, lambda _s, _p: None)

    class Now:
        def submit(self, fn: Any, *args: Any) -> None:
            fn(*args)

    monkeypatch.setattr(exam, "_pool", Now())
    monkeypatch.setattr(exam, "create_client", lambda: FakeLlm(['{"score": 99, "feedback": "Good."}']))
    r = client.post(f"/api/generations/{gid}/exam/grade", data={"qid": "q4", "part": "1", "answer": "y=F+x"})
    assert r.status_code == 202, r.text
    grade = client.get(f"/api/generations/{gid}/exam/grade/{r.json()['id']}").json()
    assert grade["status"] == "done" and grade["score"] == 6 and grade["max"] == 6  # clamped to the part's points
    assert client.post(f"/api/generations/{gid}/exam/grade", data={"qid": "q1", "answer": "A"}).status_code == 400


def test_headings_lose_the_tally_the_paper_adds_itself() -> None:
    bp = exam.clean_blueprint({"sections": [
        {"title": "一、单项选择题（共 4 题，每题 2 分，共 8 分）", "type": "single", "count": 4, "points": 2},
        {"title": "Part B (5 questions, 10 points)", "type": "fill", "count": 5, "points": 2},
        {"title": "三、简答题（Shortcut）", "type": "short", "count": 1, "points": [6]},
    ]})
    assert [s["title"] for s in bp["sections"]] == ["一、单项选择题", "Part B", "三、简答题（Shortcut）"]
