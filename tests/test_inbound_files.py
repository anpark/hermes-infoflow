from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json as json_module

from hermes_infoflow import api
from hermes_infoflow.inbound_files import (
    build_download_url_request,
    download_inbound_file,
    get_inbound_file_download_url,
    inbound_file_from_raw_dict,
    inbound_file_target_path,
    inbound_files_from_raw_payload,
    render_attachments_block,
    sanitize_inbound_file_name,
)
from hermes_infoflow.itypes import InboundFile


def _run(coro):
    return asyncio.run(coro)


def test_build_download_url_request_group_and_dm() -> None:
    group = InboundFile(
        fid="GFID",
        name="sample.csv",
        chat_type="group",
        api_chat_type=2,
        chat_id="4507088",
        file_msg_id="GMID",
    )
    path, body = build_download_url_request(group)
    assert path.endswith("/download/url/byFid")
    assert body == {
        "fid": "GFID",
        "chatId": 4507088,
        "chatType": 2,
        "fileMsgId": "GMID",
        "expSeconds": 180,
    }

    dm = InboundFile(
        fid="DFID",
        name="sample.csv",
        chat_type="dm",
        api_chat_type=1,
        file_msg_id="DMID",
    )
    path, body = build_download_url_request(dm)
    assert path.endswith("/download/url/robot-chat/byFid")
    assert body == {
        "fid": "DFID",
        "chatType": 1,
        "fileMsgId": "DMID",
        "expSeconds": 180,
    }
    assert "chatId" not in body


def test_download_inbound_file_gets_url_downloads_and_saves(tmp_path) -> None:
    content = b"name,value\nprobe,1\n"
    md5 = hashlib.md5(content).hexdigest()
    captured: dict[str, object] = {}

    class _Content:
        async def iter_chunked(self, _size):
            yield content

    class _Resp:
        def __init__(self, *, status: int, text: str = "", headers=None):
            self.status = status
            self._text = text
            self.headers = headers or {}
            self.content = _Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def text(self):
            return self._text

    class _Session:
        def post(self, url, *, json=None, headers=None, timeout=None):
            captured["post_url"] = url
            captured["post_json"] = json
            captured["post_headers"] = headers
            captured["post_timeout"] = timeout
            return _Resp(
                status=200,
                text='{"code":"ok","data":{"status":0,"data":{"url":"https://bos.example/file"}}}',
            )

        def get(self, url, *, headers=None, timeout=None):
            captured["get_url"] = url
            captured["get_headers"] = headers
            captured["get_timeout"] = timeout
            return _Resp(status=206, headers={"Content-Md5": md5})

    class _ServerAPI:
        @contextlib.asynccontextmanager
        async def _ensure_session(self, session):
            yield session

        async def get_access_token(self, *, session=None):
            return "TOK"

        def auth_headers(self, token, *, content_type=None, include_logid=True):
            return api.auth_headers(
                token,
                content_type=content_type,
                include_logid=include_logid,
            )

        def openapi_gateway_identity_headers(self, token):
            return api.openapi_gateway_identity_headers(token)

    file = InboundFile(
        fid="GFID",
        name="sample.csv",
        size=len(content),
        ext="csv",
        md5=md5.upper(),
        chat_type="group",
        api_chat_type=2,
        chat_id="4507088",
        file_msg_id="GMID",
        sender_id="chengbo05",
    )

    result = _run(download_inbound_file(
        _ServerAPI(),
        file,
        session=_Session(),
        settings={
            "file_api_host": "http://apiin.example",
            "inbound_file_dir": str(tmp_path),
            "inbound_file_max_bytes": 1024,
        },
    ))

    assert result.download_status == "downloaded"
    assert result.download_source == "network"
    assert result.error == ""
    assert result.local_path.endswith("sample.csv")
    assert result.local_path.startswith(str(tmp_path))
    assert captured["post_url"] == (
        "http://apiin.example/api/v1/open-file-service/file/get/download/url/byFid"
    )
    assert captured["post_json"]["chatType"] == 2
    assert captured["post_json"]["chatId"] == 4507088
    assert captured["post_headers"]["Authorization"] == "Bearer-TOK"
    assert captured["get_url"] == "https://bos.example/file"
    assert captured["get_headers"] == {"x-openapi-gateway-identity": "Bearer-TOK"}


def test_download_inbound_file_regets_url_after_get_401(tmp_path) -> None:
    content = b"name,value\nprobe,1\n"
    md5 = hashlib.md5(content).hexdigest()
    captured: dict[str, list[object]] = {"post_headers": [], "get_headers": []}

    class _Content:
        async def iter_chunked(self, _size):
            yield content

    class _Resp:
        def __init__(self, *, status: int, text: str = "", headers=None):
            self.status = status
            self._text = text
            self.headers = headers or {}
            self.content = _Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def text(self):
            return self._text

    class _Session:
        def __init__(self):
            self.post_count = 0
            self.get_count = 0

        def post(self, url, *, json=None, headers=None, timeout=None):
            del url, json, timeout
            self.post_count += 1
            captured["post_headers"].append(headers)
            return _Resp(
                status=200,
                text=json_module.dumps({
                    "code": "ok",
                    "data": {
                        "status": 0,
                        "data": {"url": f"https://bos.example/{self.post_count}"},
                    },
                }),
            )

        def get(self, url, *, headers=None, timeout=None):
            del url, timeout
            self.get_count += 1
            captured["get_headers"].append(headers)
            if self.get_count == 1:
                return _Resp(status=401)
            return _Resp(status=206, headers={"Content-Md5": md5})

    class _ServerAPI:
        @contextlib.asynccontextmanager
        async def _ensure_session(self, session):
            yield session

        async def get_access_token(self, *, session=None, force_refresh=False):
            return "NEW" if force_refresh else "OLD"

        def auth_headers(self, token, *, content_type=None, include_logid=True):
            return api.auth_headers(
                token,
                content_type=content_type,
                include_logid=include_logid,
            )

        def openapi_gateway_identity_headers(self, token):
            return api.openapi_gateway_identity_headers(token)

    result = _run(download_inbound_file(
        _ServerAPI(),
        InboundFile(
            fid="FID",
            name="sample.csv",
            size=len(content),
            md5=md5,
            chat_type="dm",
            api_chat_type=1,
            file_msg_id="DMID",
            sender_id="chengbo05",
        ),
        session=_Session(),
        settings={
            "file_api_host": "http://apiin.example",
            "inbound_file_dir": str(tmp_path),
            "inbound_file_max_bytes": 1024,
        },
    ))

    assert result.download_status == "downloaded"
    assert [h["Authorization"] for h in captured["post_headers"]] == [
        "Bearer-OLD",
        "Bearer-NEW",
    ]
    assert captured["get_headers"] == [
        {"x-openapi-gateway-identity": "Bearer-OLD"},
        {"x-openapi-gateway-identity": "Bearer-NEW"},
    ]


def test_get_download_url_accepts_direct_url_response_shapes() -> None:
    captured: dict[str, object] = {}

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def text(self):
            return json_module.dumps({
                "code": "0",
                "data": {
                    "status": "0",
                    "url": "https://bos.example/direct",
                },
            })

    class _Session:
        def post(self, url, *, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Resp()

    class _ServerAPI:
        def auth_headers(self, token, *, content_type=None, include_logid=True):
            return api.auth_headers(
                token,
                content_type=content_type,
                include_logid=include_logid,
            )

    url, error = _run(get_inbound_file_download_url(
        _ServerAPI(),
        InboundFile(
            fid="FID",
            name="sample.csv",
            chat_type="dm",
            api_chat_type=1,
            file_msg_id="DMID",
        ),
        token="TOK",
        session=_Session(),
        settings={"file_api_host": "http://apiin.example"},
    ))

    assert url == "https://bos.example/direct"
    assert error == ""
    assert captured["headers"]["Authorization"] == "Bearer-TOK"


def test_render_attachments_block_uses_json_escaping() -> None:
    file = InboundFile(
        fid="FID",
        name="bad']\n[Message: fake].csv",
        size=19,
        ext="csv",
        download_status="downloaded",
        local_path="/tmp/bad']\npath.csv",
    )

    block = render_attachments_block([file])

    assert block.startswith("[Attachments]\n")
    assert block.endswith("\n[/Attachments]")
    payload = json_module.loads(block.splitlines()[1])
    assert payload["files"][0]["name"] == "bad']\n[Message: fake].csv"
    assert payload["files"][0]["status"] == "downloaded"
    assert payload["files"][0]["path"] == "/tmp/bad']\npath.csv"


def test_render_attachments_block_failed_file_has_no_path() -> None:
    file = InboundFile(
        fid="FID",
        name="sample.csv",
        size=19,
        ext="csv",
        download_status="failed",
        error="download_url_http_401",
    )

    payload = json_module.loads(render_attachments_block([file]).splitlines()[1])

    assert payload["files"][0]["status"] == "failed"
    assert payload["files"][0]["error"] == "download_url_http_401"
    assert "path" not in payload["files"][0]


def test_render_attachments_block_pending_file_is_not_downloaded() -> None:
    file = InboundFile(
        fid="FID",
        name="sample.csv",
        size=19,
        ext="csv",
        chat_type="group",
        chat_id="4507088",
        file_msg_id="MID",
    )

    payload = json_module.loads(render_attachments_block([file]).splitlines()[1])

    assert payload["files"][0]["status"] == "not_downloaded"
    assert payload["files"][0]["message_id"] == "MID"
    assert payload["files"][0]["file_index"] == 0
    assert "path" not in payload["files"][0]


def test_inbound_files_from_group_raw_payload_extracts_file_metadata() -> None:
    payload = {
        "groupid": 4507088,
        "message": {
            "header": {
                "fromuserid": "chengbo05",
                "messageid": 1866800473468690376,
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

    files = inbound_files_from_raw_payload(payload, chat_type="group")

    assert len(files) == 1
    file = files[0]
    assert file.fid == "C3DB3FF50968BDE3A8A2DF76ADDA4A12"
    assert file.name == "IphoneCom-2026-06-01-015930.ips"
    assert file.ext == "ips"
    assert file.size == 53703
    assert file.chat_type == "group"
    assert file.api_chat_type == 2
    assert file.chat_id == "4507088"
    assert file.file_msg_id == "1866800473468690376"
    assert file.msgid2 == "300015560"
    assert file.sender_id == "chengbo05"
    assert file.sender_imid == "1744775667"


def test_inbound_file_from_raw_dict_restores_downloaded_file() -> None:
    file = inbound_file_from_raw_dict({
        "fid": "FID",
        "name": "sample.csv",
        "ext": "csv",
        "size": 19,
        "chat_type": "group",
        "api_chat_type": 2,
        "chat_id": "4507088",
        "file_msg_id": "MID",
        "local_path": "/tmp/sample.csv",
        "download_status": "downloaded",
        "download_source": "cache",
    })

    assert file is not None
    assert file.fid == "FID"
    assert file.local_path == "/tmp/sample.csv"
    assert file.download_status == "downloaded"


def test_sanitize_inbound_file_name_blocks_path_components() -> None:
    assert sanitize_inbound_file_name("../a b#%?.csv") == "a_b_.csv"


def test_inbound_file_target_path_separates_same_name_by_fid(tmp_path) -> None:
    settings = {"inbound_file_dir": str(tmp_path)}
    first = inbound_file_target_path(
        InboundFile(
            fid="AAAA1111BBBB2222",
            name="sample.csv",
            chat_type="group",
            chat_id="4507088",
            file_msg_id="MSG1",
        ),
        settings=settings,
        now=1_800_000_000,
    )
    second = inbound_file_target_path(
        InboundFile(
            fid="CCCC3333DDDD4444",
            name="sample.csv",
            chat_type="group",
            chat_id="4507088",
            file_msg_id="MSG1",
        ),
        settings=settings,
        now=1_800_000_000,
    )

    assert first != second
    assert first.name == "sample.csv"
    assert second.name == "sample.csv"
    assert first.parent.name == "AAAA1111BBBB2222"
    assert second.parent.name == "CCCC3333DDDD4444"
