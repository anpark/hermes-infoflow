"""Inject encrypted Infoflow webhook messages into a local Hermes gateway.

Use this when prompt/tool behavior needs to be tested end to end:

    encrypted webhook -> parser -> adapter -> Bot/LLM -> tools -> Infoflow send

The script creates small local fixtures under ``/private/tmp`` and sends inbound
messages that ask the live agent to publish or send those files. It therefore
requires a running gateway and can trigger real outbound Infoflow messages.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import time
import urllib.parse
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from _env import bootstrap, required_env, test_group_id


def _base64_url_safe_decode(value: str) -> bytes:
    normalized = str(value or "").replace("-", "+").replace("_", "/")
    normalized += "=" * ((-len(normalized)) % 4)
    return base64.b64decode(normalized)


def _encrypt_message(payload: dict[str, Any]) -> str:
    key = _base64_url_safe_decode(os.environ["INFOFLOW_ENCODING_AES_KEY"])
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padder = PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(raw) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    enc = cipher.encryptor().update(padded) + cipher.encryptor().finalize()
    return base64.b64encode(enc).decode("ascii")


def _solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    row = b"\x00" + bytes(rgb) * width
    raw = row * height

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _post_text_plain(url: str, encrypted: str) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=encrypted.encode("utf-8"),
        headers={"Content-Type": "text/plain"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _post_form(url: str, encrypted: str) -> tuple[int, str]:
    body = urllib.parse.urlencode({
        "messageJson": json.dumps({"Encrypt": encrypted}, separators=(",", ":")),
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _private_payload(
    content: str,
    *,
    message_id: int,
    from_user: str,
    from_imid: str,
) -> dict[str, Any]:
    return {
        "MsgId": message_id,
        "MsgType": "text",
        "FromUserId": from_user,
        "FromId": from_imid,
        "ToUserId": "bot",
        "CreateTime": int(time.time()),
        "Content": content,
    }


def _group_payload(
    content: str,
    *,
    message_id: int,
    group_id: str,
    from_user: str,
    from_imid: str,
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    return {
        "eventtype": "ALL_MESSAGE_FORWARD",
        "agentid": int(os.environ.get("INFOFLOW_APP_AGENT_ID", "0") or 0),
        "groupid": int(group_id),
        "fromid": from_imid,
        "message": {
            "header": {
                "toid": int(group_id),
                "totype": "GROUP",
                "msgtype": "MIXED",
                "clientmsgid": message_id - 1000,
                "messageid": str(message_id),
                "msgseqid": "",
                "fromuserid": from_user,
                "username": from_user,
                "servertime": now_ms,
                "clienttime": now_ms,
            },
            "body": [
                {"type": "TEXT", "content": content},
                {"type": "AT", "name": os.environ.get("INFOFLOW_ROBOT_NAME", "")},
            ],
        },
        "time": now_ms,
        "msgid2": message_id % 1_000_000_000,
        "messageXML": "",
    }


def _build_cases(args: argparse.Namespace, stamp: str, text_path: Path, image_path: Path) -> list[tuple[str, str, dict[str, Any], str]]:
    group_id = args.group
    base_id = int(time.time() * 1000)
    cases: list[tuple[str, str, dict[str, Any], str]] = []

    def add_private(name: str, content: str, offset: int) -> None:
        cases.append((
            name,
            "form",
            _private_payload(
                content,
                message_id=base_id + offset,
                from_user=args.from_user,
                from_imid=args.from_imid,
            ),
            content,
        ))

    def add_group(name: str, content: str, offset: int) -> None:
        cases.append((
            name,
            "text",
            _group_payload(
                content,
                message_id=base_id + offset,
                group_id=group_id,
                from_user=args.from_user,
                from_imid=args.from_imid,
            ),
            content,
        ))

    if args.case in ("dm-file", "all"):
        add_private(
            "dm-file",
            (
                f"【PROMPTSIM|{stamp}|dm-file】"
                f"请把这个本地文本文件通过 Infoflow 以链接发给我：{text_path}。"
                "只需发链接，不要解释实现。"
            ),
            1,
        )
    if args.case in ("dm-group-md-image", "all"):
        add_private(
            "dm-group-md-image",
            (
                f"【PROMPTSIM|{stamp}|dm-group-md-image】"
                f"请把这个本地图片通过 Markdown 图片形式发到群 {group_id}：{image_path}。"
                "只发送图片，不要把本地路径发出去。"
            ),
            2,
        )
    if args.case in ("group-native-image", "all"):
        add_group(
            "group-native-image",
            (
                f"【PROMPTSIM|{stamp}|group-native-image】"
                f"请把这个本地图片作为如流原生图片发在当前群：{image_path}。"
            ),
            3,
        )
    if args.case in ("dm-url-md-image", "all"):
        add_private(
            "dm-url-md-image",
            (
                f"【PROMPTSIM|{stamp}|dm-url-md-image】"
                "请把这个网络图片 URL 通过 Markdown 图片形式发到群 "
                f"{group_id}：{args.image_url}。不要改成普通文本链接。"
            ),
            4,
        )
    return cases


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        choices=[
            "dm-file",
            "dm-group-md-image",
            "group-native-image",
            "dm-url-md-image",
            "all",
        ],
        default="all",
    )
    parser.add_argument("--group", default="", help="Target/test group id.")
    parser.add_argument("--from-user", default="chengbo05", help="Simulated sender uuapName.")
    parser.add_argument("--from-imid", default="1744775667", help="Simulated sender imid.")
    parser.add_argument("--port", type=int, default=0, help="Gateway port; defaults to INFOFLOW_PORT.")
    parser.add_argument(
        "--webhook-path",
        default="",
        help="Gateway webhook path; defaults to INFOFLOW_WEBHOOK_PATH.",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=8.0,
        help="Delay between cases when --case all is used.",
    )
    parser.add_argument(
        "--workdir",
        default="/private/tmp/infoflow-prompt-inbound",
        help="Directory for generated local fixtures.",
    )
    parser.add_argument(
        "--image-url",
        default="https://cb-storage.oss-cn-beijing.aliyuncs.com/testuse/example.jpg",
        help="HTTP/HTTPS image URL used by dm-url-md-image.",
    )
    return parser


def main() -> int:
    bootstrap()
    required_env("INFOFLOW_ENCODING_AES_KEY")
    args = _build_parser().parse_args()
    args.group = args.group or test_group_id()
    port = args.port or int(os.environ.get("INFOFLOW_PORT") or "26009")
    webhook_path = args.webhook_path or os.environ.get("INFOFLOW_WEBHOOK_PATH") or "/webhook/infoflow"
    url = f"http://127.0.0.1:{port}{webhook_path}"

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work_dir = Path(args.workdir).expanduser() / stamp
    work_dir.mkdir(parents=True, exist_ok=True)
    text_path = work_dir / f"prompt-file-{stamp}.txt"
    text_path.write_text(f"PROMPTSIM file_delivery prompt test {stamp}\n", encoding="utf-8")
    image_path = work_dir / f"prompt-image-{stamp}.png"
    image_path.write_bytes(_solid_png(260, 180, (210, 80, 30)))

    sent = []
    for name, kind, payload, content in _build_cases(args, stamp, text_path, image_path):
        encrypted = _encrypt_message(payload)
        status, body = (
            _post_form(url, encrypted)
            if kind == "form"
            else _post_text_plain(url, encrypted)
        )
        sent.append({
            "case": name,
            "status": status,
            "body": body,
            "marker": content.split("】", 1)[0] + "】",
            "message_id": (
                payload.get("MsgId")
                or payload.get("message", {}).get("header", {}).get("messageid")
            ),
        })
        if args.case == "all" and args.delay_seconds > 0:
            time.sleep(args.delay_seconds)

    print(json.dumps({
        "ok": all(item["status"] == 200 for item in sent),
        "stamp": stamp,
        "webhook": url,
        "group": args.group,
        "from_user": args.from_user,
        "text_path": str(text_path),
        "image_path": str(image_path),
        "sent": sent,
    }, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] == 200 for item in sent) else 1


if __name__ == "__main__":
    raise SystemExit(main())
