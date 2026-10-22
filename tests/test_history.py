import asyncio
import json
import sqlite3
from dataclasses import replace

import pytest
import pytest_asyncio
from pydantic_ai.messages import ModelMessagesTypeAdapter

from omnivoice.actions import ActionPlan, InsertTextAction
from omnivoice.chat_ui import SessionPicker, replay
from omnivoice.history import ChatHistory, HistoryError, HistoryUnavailable, render_turn, safe_text


@pytest_asyncio.fixture
async def history(tmp_path):
    service = ChatHistory(tmp_path / "history.sqlite3")
    await service.start()
    try:
        yield service
    finally:
        await service.close()


async def add_turn(history, text="hello", selected="source", plan=None):
    ident = await history.begin(history.active.id, text, selected, "test:model")
    plan = plan or ActionPlan(actions=(InsertTextAction(type="insert_text", text="line one\nline two"),))
    await history.save_plan(ident, plan)
    return await history.finish(ident, "completed", None, len(plan.actions), False)


@pytest.mark.asyncio
async def test_resume_rename_and_isolate_sessions(history):
    first = history.active
    await add_turn(history, "first")
    second = await history.create("CV editing")
    await add_turn(history, "second")
    await history.rename("Updated CV")
    assert history.active.id == second.id
    assert [s.id for s in await history.list_sessions()] == [second.id, first.id]
    assert [t["request"] for t in await history.turns(first.id)] == ["first"]
    await history.close()
    reopened = ChatHistory(history.path)
    try:
        await reopened.start()
        assert reopened.active.name == "Updated CV"
        assert reopened.active.id == second.id
        await reopened.activate(first.id[:8])
        assert (await reopened.turns(first.id))[0]["request"] == "first"
    finally:
        await reopened.close()
    # Fixture close should remain safe after an explicit close.


@pytest.mark.asyncio
async def test_delete_cascades_and_selects_fallback(history):
    first = history.active
    await add_turn(history)
    second = await history.create("duplicate")
    await history.create("duplicate")
    assert await history.delete(history.active.id)
    assert history.active.id == second.id
    assert not await history.delete(first.id)
    assert await history.turns(first.id) == []
    await history.delete(second.id)
    assert history.active.name == "New chat"
    assert len(await history.list_sessions()) == 1


@pytest.mark.asyncio
async def test_bad_identifiers_and_names_do_not_disable_storage(history):
    for prefix in ["", "123", "%" * 8, "missing-id"]:
        with pytest.raises(HistoryError):
            await history.activate(prefix)
    for name in ["", "\x1b[2J", "x" * 121]:
        with pytest.raises(HistoryError):
            await history.rename(name)
    assert history.healthy


@pytest.mark.asyncio
async def test_ambiguous_prefix_rejected(history):
    await history._call(lambda: history._db().execute(
        "INSERT INTO sessions VALUES ('aaaaaaaa-one','one','1','1')"))
    await history._call(lambda: history._db().execute(
        "INSERT INTO sessions VALUES ('aaaaaaaa-two','two','1','1')"))
    with pytest.raises(HistoryError, match="ambiguous"):
        await history.resolve("aaaaaaaa")


@pytest.mark.asyncio
async def test_history_round_trip_outcomes_and_whole_turn_budget(history):
    first = await add_turn(history, "first", selected="explicit private selection")
    second = await add_turn(history, "second")
    all_messages, omitted = await history.message_history(history.active.id, second["id"] + 1, 100_000)
    serialized = ModelMessagesTypeAdapter.dump_json(all_messages)
    assert not omitted
    assert b"explicit private selection" in serialized
    assert b"execution_result" in serialized
    assert b"completed_actions" in serialized
    assert b"provider_response_id" not in serialized or b'"provider_response_id":null' in serialized
    assert len(all_messages) == 6
    latest_cost = (len(second["messages_json"].encode()) + 2) // 3
    messages, omitted = await history.message_history(history.active.id, second["id"] + 1, latest_cost)
    assert omitted and len(messages) == 3
    assert b"first" not in ModelMessagesTypeAdapter.dump_json(messages)
    assert (await history.message_history(history.active.id, first["id"], 0)) == ([], False)
    assert await history.message_history(history.active.id, second["id"] + 1, 0) == ([], True)


@pytest.mark.asyncio
async def test_replay_prints_every_turn_in_batches_and_ends_at_latest(history):
    for i in range(55):
        await add_turn(history, f"request {i}")
    writes = []
    await replay(history, history.active, writes.append)
    transcript = "".join(writes)
    assert transcript.count("You:\n") == 55
    assert transcript.count("AI:\n") == 55
    assert transcript.index("request 0\n") < transcript.index("request 54\n")
    assert "line one\nline two" in transcript
    assert writes[-1].startswith("\nYou:\nrequest 54\n")
    rows = await history.turns(history.active.id, limit=100)
    assert writes[1] == render_turn(rows[0])


@pytest.mark.asyncio
async def test_empty_and_failed_conversation_rendering(history):
    writes = []
    await replay(history, history.active, writes.append)
    assert "No messages yet." in "".join(writes)
    turn_id = await history.begin(history.active.id, "request\x1b[2J", None, "test:model")
    turn = await history.finish(turn_id, "failed", "planning_failed", 0, False)
    rendered = render_turn(turn)
    assert "AI:" not in rendered
    assert "Result: failed" in rendered
    assert "\x1b" not in rendered and "\\x1b" in rendered
    assert safe_text("a\nb\r\t\u202e") == "a\nb\\r\\t\\u202e"


@pytest.mark.asyncio
async def test_crash_recovery_does_not_claim_execution_success(tmp_path):
    path = tmp_path / "history.sqlite3"
    original = ChatHistory(path)
    await original.start()
    planning = await original.begin(original.active.id, "planning", None, "test:model")
    executing = await original.begin(original.active.id, "executing", None, "test:model")
    await original.save_plan(executing, ActionPlan(actions=(InsertTextAction(type="insert_text", text="x"),)))
    await original.close()
    restored = ChatHistory(path)
    try:
        await restored.start()
        turns = await restored.turns(restored.active.id)
        assert turns[0]["id"] == planning
        assert turns[0]["failure_category"] == "interrupted_before_execution"
        assert turns[1]["failure_category"] == "execution_unknown"
        assert turns[1]["possibly_partial"]
        assert all(t["status"] == "interrupted" for t in turns)
        assert b"execution_unknown" in turns[1]["messages_json"].encode()
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_single_instance_lock_and_release(history):
    other = ChatHistory(history.path)
    try:
        with pytest.raises(HistoryUnavailable, match="Another"):
            await other.start()
        assert not other.healthy
        await history.close()
        await other.retry()
        assert other.healthy
    finally:
        await other.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["corrupt", "future"])
async def test_bad_database_is_preserved(tmp_path, kind):
    path = tmp_path / "history.sqlite3"
    if kind == "corrupt":
        path.write_bytes(b"not a database")
    else:
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA user_version=99")
    before = path.read_bytes()
    service = ChatHistory(path)
    try:
        with pytest.raises(HistoryError):
            await service.start()
        assert not service.healthy
        assert path.read_bytes() == before
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_database_lock_is_bounded_and_retry_recovers(history):
    db = sqlite3.connect(history.path)
    try:
        db.execute("BEGIN EXCLUSIVE")
        with pytest.raises(HistoryUnavailable):
            await asyncio.wait_for(history.begin(history.active.id, "test", None, "test:model"), 2)
        assert not history.healthy
    finally:
        db.rollback()
        db.close()
    await history.retry()
    assert history.healthy


@pytest.mark.asyncio
async def test_failed_outcome_write_is_retried_without_a_new_turn(history, monkeypatch):
    ident = await history.begin(history.active.id, "test", None, "test:model")
    original = history._finish
    def fail(*args):
        raise sqlite3.OperationalError("disk unavailable")
    monkeypatch.setattr(history, "_finish", fail)
    with pytest.raises(HistoryUnavailable):
        await history.finish(ident, "cancelled", "cancelled", 1, True)
    assert not history.healthy
    monkeypatch.setattr(history, "_finish", original)
    await history.retry()
    turns = await history.turns(history.active.id)
    assert len(turns) == 1
    assert turns[0]["status"] == "cancelled"
    assert turns[0]["completed_actions"] == 1
    assert turns[0]["possibly_partial"]
    assert history._pending is None and history.healthy


def test_picker_filters_navigates_and_handles_empty_matches():
    from omnivoice.history import ChatSession
    first = ChatSession("first-id", "CV editing", "1", "2", "rewrite")
    second = replace(first, id="second-id", name="Python", preview="decorators")
    picker = SessionPicker([first, second])
    picker.move(1)
    assert picker.selected() == second.id
    picker.filter("cv")
    assert picker.selected() == first.id
    picker.filter("DECORATORS")
    assert picker.selected() == second.id
    picker.filter("no matches")
    picker.move(-1)
    assert picker.selected() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("keys,expected", [("\x1b[B\r", "second-id"), ("\x1b", None), ("Python\r", "second-id")])
async def test_picker_real_key_bindings_use_owned_input_only(keys, expected):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from omnivoice.history import ChatSession
    sessions = [ChatSession("first-id", "CV", "1", "1"), ChatSession("second-id", "Python", "1", "2")]
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            task = asyncio.create_task(SessionPicker(sessions).run())
            await asyncio.sleep(0.05)
            pipe.send_text(keys)
            assert await asyncio.wait_for(task, 3) == expected


@pytest.mark.asyncio
async def test_corrupt_message_history_pauses_ai_without_erasing_data(history):
    turn = await add_turn(history)
    def corrupt():
        with history._db():
            history._db().execute("UPDATE turns SET messages_json='not json' WHERE id=?", (turn["id"],))
    await history._call(corrupt)
    with pytest.raises(HistoryUnavailable):
        await history.message_history(history.active.id, turn["id"] + 1, 10000)
    assert not history.healthy


@pytest.mark.asyncio
async def test_migration_rolls_back_when_existing_unversioned_schema_conflicts(tmp_path):
    path = tmp_path / "history.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE turns (existing TEXT)")
    history = ChatHistory(path)
    try:
        with pytest.raises(HistoryUnavailable):
            await history.start()
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 0
            assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("turns",)]
    finally:
        await history.close()
