from __future__ import annotations

import asyncio

from hermes_infoflow import api as api_mod
from hermes_infoflow import serverapi as serverapi_mod
from hermes_infoflow.serverapi import ServerAPI


def _settings() -> dict[str, object]:
    return {
        "api_host": "https://api.im.baidu.com",
        "app_key": "k",
        "app_secret": "s",
        "check_token": "tok",
        "encoding_aes_key": "aes",
        "robot_name": "helper",
        "robot_id": "999",
        "app_agent_id": 6471,
    }


def test_serverapi_bos_upload_delegates_to_api(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_upload(account, **kwargs):
        captured["account"] = account
        captured.update(kwargs)
        return api_mod.BosUploadResult(True, object_key="obj", e_tag="etag")

    monkeypatch.setattr(serverapi_mod._api, "im_bos_upload", fake_upload)
    service = ServerAPI(settings=_settings())

    result = asyncio.run(service.bos_upload(
        file_content=b"abc",
        file_name="a.bin",
        object_key="hermes/a.bin",
        session=object(),
        timeout=7,
    ))

    assert result == api_mod.BosUploadResult(True, object_key="obj", e_tag="etag")
    account = captured["account"]
    assert isinstance(account, api_mod.InfoflowAccountAPI)
    assert account.api_host == "https://api.im.baidu.com"
    assert captured["file_content"] == b"abc"
    assert captured["file_name"] == "a.bin"
    assert captured["object_key"] == "hermes/a.bin"
    assert captured["timeout"] == 7


def test_serverapi_bos_get_url_delegates_to_api(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_get_url(account, **kwargs):
        captured["account"] = account
        captured.update(kwargs)
        return api_mod.BosGetUrlResult(
            True,
            url="https://download.example.com/a.bin",
            expiration_seconds=7200,
        )

    monkeypatch.setattr(serverapi_mod._api, "im_bos_get_url", fake_get_url)
    service = ServerAPI(settings=_settings())

    result = asyncio.run(service.bos_get_url(
        object_key="hermes/a.bin",
        expiration_seconds=7200,
        session=object(),
        timeout=8,
    ))

    assert result == api_mod.BosGetUrlResult(
        True,
        url="https://download.example.com/a.bin",
        expiration_seconds=7200,
    )
    account = captured["account"]
    assert isinstance(account, api_mod.InfoflowAccountAPI)
    assert account.app_key == "k"
    assert captured["object_key"] == "hermes/a.bin"
    assert captured["expiration_seconds"] == 7200
    assert captured["timeout"] == 8
