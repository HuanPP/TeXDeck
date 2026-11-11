"""What the model remembers about a user: parsing its notes, storing, using them."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, deck, memory, store
from app.config import settings
from app.main import _run_batch, app
from tests.conftest import FakeLlm


def test_split_reply_takes_the_block_out() -> None:
    reply = (
        "%% FRAME 1\n\\begin{frame}{A}\nx\n\\end{frame}\n\n<memory>\n"
        "habit: 每页要点不超过 4 条\n- mistake：柱状图标签不能重叠\nnot an entry\nhabits: Prefers bar charts\n</memory>\n"
    )
    clean, found = memory.split_reply(reply)
    assert "<memory>" not in clean and clean.rstrip().endswith("\\end{frame}")
    assert found == {"habits": ["每页要点不超过 4 条", "Prefers bar charts"], "mistakes": ["柱状图标签不能重叠"]}
    assert memory.split_reply("plain") == ("plain", {"habits": [], "mistakes": []})


def test_remember_skips_repeats_and_keeps_the_newest() -> None:
    memory.remember({"habits": ["No agenda slide."], "mistakes": []})
    memory.remember({"habits": ["no agenda slide", "Short titles"], "mistakes": ["Labels overlap"]})
    assert memory.load() == {"habits": ["No agenda slide.", "Short titles"], "mistakes": ["Labels overlap"]}
    memory.remember({"habits": [f"habit {i}" for i in range(memory.MAX_LINES + 5)]})
    habits = memory.load()["habits"]
    assert len(habits) == memory.MAX_LINES and habits[-1] == f"habit {memory.MAX_LINES + 4}"


def test_each_account_has_its_own_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "users_file", tmp_path / "users.json")
    auth.run_as("alice", memory.remember, {"habits": ["Alice likes tables"]})
    assert auth.run_as("alice", memory.load)["habits"] == ["Alice likes tables"]
    assert auth.run_as("bob", memory.load)["habits"] == []
    assert (tmp_path / "accounts" / "alice" / "memory.json").is_file()


def test_generation_prompt_carries_the_memory() -> None:
    memory.save({"habits": ["每页要点不超过 4 条"], "mistakes": ["箭头要够长"]})
    template = deck.builtin_template("navy")
    request = deck.DeckRequest(template_key="navy", title="T", source_text="Notes.")
    messages = deck.build_generation_messages(template, request, [], None, memory.note())
    assert "每页要点不超过 4 条" in messages[2]["content"] and "箭头要够长" in messages[2]["content"]
    plain = deck.build_generation_messages(template, request, [], None, "")
    assert "habits" not in plain[2]["content"]


_TEX = r"""\documentclass[aspectratio=169,10pt]{beamer}
\usepackage[zh,bjtu]{collegebeamer}
\begin{document}
\begin{frame}{A}
\item one
\end{frame}
\end{document}
"""


def test_a_batch_learns_from_the_reply(
    artifact_root: Path, fake_xelatex_ok: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    memory.save({"habits": ["Short titles"], "mistakes": []})
    meta = store.new_generation(title="T", template_key="navy", template_name="Navy",
                                instruction="", source_kind="text", source_names=[])
    (store.generation_dir(meta.id) / "main.tex").write_text(_TEX, encoding="utf-8")
    fake = FakeLlm(["%% FRAME 1\n\\begin{frame}{A}\n\\item 1\n\\end{frame}\n"
                    "<memory>\nhabit: Write numbers as digits\n</memory>\n"])
    monkeypatch.setattr("app.main.create_client", lambda on_usage=None: fake)
    _run_batch(meta.id, [], [(1, "use digits")], lambda *_: None)
    sent = fake.calls[0][2]["content"]
    assert "Short titles" in sent and "<memory>" in sent  # knows what is kept, may add
    assert memory.load()["habits"] == ["Short titles", "Write numbers as digits"]
    out = (store.generation_dir(meta.id) / "main.tex").read_text(encoding="utf-8")
    assert "<memory>" not in out and "\\item 1" in out


def test_memory_endpoints(artifact_root: Path) -> None:
    with TestClient(app) as client:
        assert client.get("/api/memory").json() == {"habits": [], "mistakes": []}
        saved = client.put("/api/memory", json={"habits": [" A ", "a", ""], "mistakes": ["B"]}).json()
        assert saved == {"habits": ["A"], "mistakes": ["B"]}
        assert client.get("/api/memory").json() == saved
