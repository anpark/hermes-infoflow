"""Tests for ``infoflow_get_message_history``."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_infoflow import message_store as ms
from hermes_infoflow import tools
from hermes_infoflow.bot import recall_inbound_message_id_hint_scope
from hermes_infoflow.message_store import MessageStore


def _today_at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime.now().astimezone().replace(
        hour=hour,
        minute=minute,
        second=second,
        microsecond=0,
    )


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _time_arg(dt: datetime) -> str:
    return (
        f"{dt.year}.{dt.month:02d}.{dt.day:02d} "
        f"{dt.hour:02d}.{dt.minute:02d}.{dt.second:02d}"
    )


def _adapter_for(store: MessageStore, *, admin_uid: str = "admin") -> SimpleNamespace:
    def _lookup(key: str) -> str:
        if key.startswith("user:"):
            rec = store.find_user_by_user_id(key.removeprefix("user:"))
            return rec.name if rec else ""
        if key.startswith("bot:"):
            rec = store.find_bot_by_agent_id(key.removeprefix("bot:"))
            return rec.name if rec else ""
        return ""

    return SimpleNamespace(
        _message_store=store,
        _admin_uid=admin_uid,
        _participant_name_for_key=_lookup,
    )


def test_history_tool_message_window_returns_json_array(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    store.upsert_participant(participant_type="user", user_id="alice", name="Alice")
    base_dt = _today_at(9, 20)
    created_times: dict[str, datetime] = {}
    for idx, (mid, sender) in enumerate(
        (("m1", "user:bob"), ("m2", "user:alice"), ("m3", "user:admin")),
        start=1,
    ):
        created = base_dt.replace(minute=20 + idx, second=idx)
        created_times[mid] = created
        store.persist_group(
            message_id=mid,
            group_id="4507088",
            sender=sender,
            content=f"text {idx}",
            created_time=_ms(created),
        )

    adapter = _adapter_for(store)
    monkeypatch.setattr(tools, "_get_live_adapter", lambda: adapter)

    handler = tools.make_history_handler()
    with recall_inbound_message_id_hint_scope("m3"):
        result = asyncio.run(handler({
            "message_id": "m2",
            "before_count": 1,
            "after_count": 1,
        }))

    parsed = json.loads(result)
    assert isinstance(parsed, list)
    assert [item["content"].splitlines()[2] for item in parsed] == [
        f"[Message: message_id:'m1'; created_time:'{_time_arg(created_times['m1'])}']",
        f"[Message: message_id:'m2'; created_time:'{_time_arg(created_times['m2'])}']",
        f"[Message: message_id:'m3'; created_time:'{_time_arg(created_times['m3'])}']",
    ]
    assert (
        "[Sender: type:'human'; user_id:'alice'; name:'Alice'; permission:'restricted']"
        in parsed[1]["content"]
    )
    assert "[Unread Message Context:" not in parsed[1]["content"]
    assert "[Handling Strategy]" not in parsed[1]["content"]


def test_history_tool_persists_group_file_metadata_without_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    file_raw = {
        "eventtype": "ALL_MESSAGE_FORWARD",
        "groupid": 4507088,
        "message": {
            "header": {
                "fromuserid": "chengbo05",
                "messageid": "1866800473468690376",
            },
            "body": [{
                "type": "FILE",
                "name": "IphoneCom-2026-06-01-015930.ips",
                "fid": "C3DB3FF50968BDE3A8A2DF76ADDA4A12",
                "size": 53703,
                "md5": "",
            }],
        },
        "fromid": 1744775667,
        "msgid2": 300015560,
    }
    created = _today_at(21, 14, 25)
    store.persist_group(
        message_id="1866800473468690376",
        group_id="4507088",
        sender="user:chengbo05",
        content="",
        created_time=_ms(created),
        msg_id2="300015560",
        raw_json=json.dumps(file_raw),
    )
    store.persist_group(
        message_id="1866800481628708809",
        group_id="4507088",
        sender="user:chengbo05",
        content="@bot 分析下这个文件",
        created_time=_ms(_today_at(21, 14, 33)),
    )

    adapter = _adapter_for(store)
    monkeypatch.setattr(tools, "_get_live_adapter", lambda: adapter)

    handler = tools.make_history_handler()
    with recall_inbound_message_id_hint_scope("1866800481628708809"):
        result = asyncio.run(handler({
            "message_id": "1866800481628708809",
            "before_count": 1,
            "after_count": 0,
        }))

    parsed = json.loads(result)
    first = parsed[0]["content"]
    assert "[Attachments]\n" in first
    assert first.index("[Attachments]") < first.index("[Message:")
    assert '"name":"IphoneCom-2026-06-01-015930.ips"' in first
    assert '"status":"not_downloaded"' in first
    assert '"message_id":"1866800473468690376"' in first
    assert '"file_index":0' in first
    assert '"path"' not in first

    stored_raw = json.loads(
        store.find_any("1866800473468690376").raw_json  # type: ignore[union-attr]
    )
    assert stored_raw["_hermes_infoflow_files"][0]["download_status"] == "not_downloaded"


def test_download_attachment_tool_downloads_and_persists_group_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    store.persist_group(
        message_id="file-msg",
        group_id="4507088",
        sender="user:chengbo05",
        content="",
        created_time=_ms(_today_at(21, 14, 25)),
        msg_id2="300015560",
        raw_json=json.dumps({
            "groupid": 4507088,
            "message": {
                "header": {
                    "fromuserid": "chengbo05",
                    "messageid": "file-msg",
                },
                "body": [{
                    "type": "FILE",
                    "name": "sample.csv",
                    "fid": "FID",
                    "size": 19,
                }],
            },
            "msgid2": 300015560,
        }),
    )
    store.persist_group(
        message_id="current",
        group_id="4507088",
        sender="user:chengbo05",
        content="@bot 分析下这个文件",
        created_time=_ms(_today_at(21, 14, 33)),
    )

    class FakeServerAPI:
        async def download_inbound_file(self, file, *, session=None):
            assert session is None
            assert file.chat_type == "group"
            assert file.chat_id == "4507088"
            assert file.file_msg_id == "file-msg"
            file.download_status = "downloaded"
            file.download_source = "network"
            file.local_path = str(tmp_path / "sample.csv")
            return file

    adapter = _adapter_for(store)
    adapter._serverapi = FakeServerAPI()
    adapter._http_session = object()
    monkeypatch.setattr(tools, "_get_live_adapter", lambda: adapter)

    handler = tools.make_download_attachment_handler()
    with recall_inbound_message_id_hint_scope("current"):
        result = asyncio.run(handler({
            "message_id": "file-msg",
            "file_index": 0,
        }))

    parsed = json.loads(result)
    assert parsed["success"] is True
    assert parsed["status"] == "downloaded"
    assert parsed["path"] == str(tmp_path / "sample.csv")

    stored_raw = json.loads(
        store.find_any("file-msg").raw_json  # type: ignore[union-attr]
    )
    assert stored_raw["_hermes_infoflow_files"][0]["download_status"] == "downloaded"
    assert stored_raw["_hermes_infoflow_files"][0]["local_path"] == str(tmp_path / "sample.csv")


def test_download_image_tool_downloads_and_persists_group_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    image_url = "http://e4hi.im.baidu.com/proxy/download?taskId=T&agentId=6471"
    store.persist_group(
        message_id="image-msg",
        group_id="4507088",
        sender="user:chengbo05",
        content="<media:image>",
        created_time=_ms(_today_at(21, 14, 25)),
        raw_json=json.dumps({
            "groupid": 4507088,
            "message": {
                "body": [{
                    "type": "IMAGE",
                    "downloadurl": image_url,
                }],
            },
        }),
    )
    store.persist_group(
        message_id="current",
        group_id="4507088",
        sender="user:chengbo05",
        content="@bot 看看历史图片",
        created_time=_ms(_today_at(21, 14, 33)),
    )

    class FakeServerAPI:
        async def get_access_token(self):
            return "TOKEN"

    async def fake_download(url, *, token_provider, session=None, max_bytes=0):
        assert url == image_url
        assert await token_provider() == "TOKEN"
        assert session is None
        return b"\xff\xd8\xfffake-jpeg", ".jpg"

    cached_path = tmp_path / "cached.jpg"

    def fake_cache(data: bytes, ext: str) -> str:
        assert data.startswith(b"\xff\xd8\xff")
        assert ext == ".jpg"
        cached_path.write_bytes(data)
        return str(cached_path)

    adapter = _adapter_for(store)
    adapter._serverapi = FakeServerAPI()
    monkeypatch.setattr(tools, "_get_live_adapter", lambda: adapter)
    monkeypatch.setattr(tools, "_download_inbound_image", fake_download)
    monkeypatch.setattr(tools, "_cache_image_bytes_for_vision", fake_cache)

    handler = tools.make_download_image_handler()
    with recall_inbound_message_id_hint_scope("current"):
        result = asyncio.run(handler({
            "message_id": "image-msg",
            "image_index": 0,
        }))

    parsed = json.loads(result)
    assert parsed["success"] is True
    assert parsed["status"] == "downloaded"
    assert parsed["path"] == str(cached_path)
    assert parsed["mime_type"] == "image/jpeg"
    assert "vision_analyze" in parsed["next_step"]

    stored_raw = json.loads(
        store.find_any("image-msg").raw_json  # type: ignore[union-attr]
    )
    stored_image = stored_raw["_hermes_infoflow_images"][0]
    assert stored_image["status"] == "downloaded"
    assert stored_image["path"] == str(cached_path)
    assert stored_image["mime_type"] == "image/jpeg"


def test_analyze_image_tool_downloads_then_runs_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_download_image_for_tool(*, message_id, image_index, force=False):
        assert message_id == "image-msg"
        assert image_index == 1
        assert force is False
        return {
            "success": True,
            "message_id": message_id,
            "image_index": image_index,
            "status": "downloaded",
            "path": "/tmp/hermes-image.jpg",
            "mime_type": "image/jpeg",
            "size": 123,
        }

    async def fake_vision(path: str, user_prompt: str):
        assert path == "/tmp/hermes-image.jpg"
        assert user_prompt == "识别水果成熟度"
        return {"success": True, "analysis": "图中是苹果，接近成熟。"}

    monkeypatch.setattr(tools, "_download_image_for_tool", fake_download_image_for_tool)
    monkeypatch.setattr(tools, "_vision_analyze_image_path", fake_vision)

    handler = tools.make_analyze_image_handler()
    result = asyncio.run(handler({
        "message_id": "image-msg",
        "image_index": 1,
        "user_prompt": "识别水果成熟度",
    }))

    parsed = json.loads(result)
    assert parsed == {
        "success": True,
        "message_id": "image-msg",
        "image_index": 1,
        "status": "analyzed",
        "analysis": "图中是苹果，接近成熟。",
        "mime_type": "image/jpeg",
        "size": 123,
    }


def test_download_attachment_tool_rejects_invalid_file_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    store.persist_group(
        message_id="file-msg",
        group_id="4507088",
        sender="user:chengbo05",
        content="",
        created_time=_ms(_today_at(21, 14, 25)),
        raw_json=json.dumps({
            "_hermes_infoflow_files": [{
                "fid": "FID",
                "name": "sample.csv",
                "chat_type": "group",
                "chat_id": "4507088",
                "file_msg_id": "file-msg",
                "download_status": "not_downloaded",
            }]
        }),
    )
    store.persist_group(
        message_id="current",
        group_id="4507088",
        sender="user:chengbo05",
        content="@bot 分析下这个文件",
        created_time=_ms(_today_at(21, 14, 33)),
    )

    class FakeServerAPI:
        async def download_inbound_file(self, file, *, session=None):
            raise AssertionError("invalid file_index must not download")

    adapter = _adapter_for(store)
    adapter._serverapi = FakeServerAPI()
    monkeypatch.setattr(tools, "_get_live_adapter", lambda: adapter)

    handler = tools.make_download_attachment_handler()
    with recall_inbound_message_id_hint_scope("current"):
        result = asyncio.run(handler({
            "message_id": "file-msg",
            "file_index": -1,
        }))

    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "file_index must be a non-negative integer" in parsed["error"]


def test_history_tool_rejects_cross_conversation_for_restricted_sender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    store.persist_group(
        message_id="current",
        group_id="4507088",
        sender="user:bob",
        content="current",
        created_time=_ms(_today_at(9, 30)),
    )
    store.persist_group(
        message_id="other",
        group_id="999",
        sender="user:alice",
        content="other",
        created_time=_ms(_today_at(9, 31)),
    )

    monkeypatch.setattr(tools, "_get_live_adapter", lambda: _adapter_for(store))
    handler = tools.make_history_handler()
    with recall_inbound_message_id_hint_scope("current"):
        result = asyncio.run(handler({"target": "group:999"}))

    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "Only admin" in parsed["error"]


def test_history_tool_allows_admin_explicit_target_time_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    store.upsert_participant(participant_type="user", user_id="alice", name="Alice")
    today = _today_at(9, 31, 45)
    store.persist_group(
        message_id="current",
        group_id="4507088",
        sender="user:carol",
        content="current",
        created_time=_ms(_today_at(9, 30)),
    )
    store.persist_group(
        message_id="other",
        group_id="999",
        sender="user:alice",
        content="other",
        created_time=_ms(today),
    )

    monkeypatch.setattr(
        tools,
        "_get_live_adapter",
        lambda: _adapter_for(store, admin_uid="admin,carol"),
    )
    handler = tools.make_history_handler()
    with recall_inbound_message_id_hint_scope("current"):
        result = asyncio.run(handler({
            "target": "infoflow:group:999",
            "start_time": _time_arg(today),
            "end_time": _time_arg(today),
        }))

    parsed = json.loads(result)
    assert len(parsed) == 1
    assert parsed[0]["time"] == _time_arg(today)
    assert f"[Message: message_id:'other'; created_time:'{_time_arg(today)}']" in parsed[0]["content"]


def test_history_tool_end_time_is_second_inclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    current = _today_at(9, 30)
    included = _today_at(19, 56, 59)
    excluded = _today_at(19, 57, 0)
    store.persist_group(
        message_id="current",
        group_id="4507088",
        sender="user:admin",
        content="current",
        created_time=_ms(current),
    )
    store.persist_group(
        message_id="included",
        group_id="999",
        sender="user:alice",
        content="included",
        created_time=_ms(included) + 999,
    )
    store.persist_group(
        message_id="excluded",
        group_id="999",
        sender="user:alice",
        content="excluded",
        created_time=_ms(excluded),
    )

    monkeypatch.setattr(tools, "_get_live_adapter", lambda: _adapter_for(store))
    handler = tools.make_history_handler()
    with recall_inbound_message_id_hint_scope("current"):
        result = asyncio.run(handler({
            "target": "group:999",
            "start_time": _time_arg(included),
            "end_time": _time_arg(included),
        }))

    parsed = json.loads(result)
    assert [item["content"].splitlines()[2] for item in parsed] == [
        f"[Message: message_id:'included'; created_time:'{_time_arg(included)}']"
    ]


def test_history_tool_invalid_time_returns_json_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    store.persist_group(
        message_id="current",
        group_id="4507088",
        sender="user:admin",
        content="current",
        created_time=_ms(_today_at(9, 30)),
    )

    monkeypatch.setattr(tools, "_get_live_adapter", lambda: _adapter_for(store))
    handler = tools.make_history_handler()
    with recall_inbound_message_id_hint_scope("current"):
        result = asyncio.run(handler({"start_time": "2026.99.99"}))

    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "start_time must use format" in parsed["error"]


def test_history_tool_message_id_ignores_time_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    created = _today_at(9, 30, 1)
    store.persist_group(
        message_id="current",
        group_id="4507088",
        sender="user:bob",
        content="current",
        created_time=_ms(created),
    )

    monkeypatch.setattr(tools, "_get_live_adapter", lambda: _adapter_for(store))
    handler = tools.make_history_handler()
    with recall_inbound_message_id_hint_scope("current"):
        result = asyncio.run(handler({
            "message_id": "current",
            "start_time": "invalid",
            "date": "2026.99.99",
        }))

    parsed = json.loads(result)
    assert len(parsed) == 1
    assert f"[Message: message_id:'current'; created_time:'{_time_arg(created)}']" in parsed[0]["content"]
