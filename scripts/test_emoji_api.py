#!/usr/bin/env python3
"""Test Infoflow emoji add/remove API.

Supports both group (chatType=2) and DM (chatType=7) scenarios.

Usage:
  # Group (default)
  python test_emoji_api.py

  # DM
  TEST_CHAT_TYPE=dm TEST_BASE_MSG_ID=xxx TEST_FROM_UID=xxx python test_emoji_api.py

  # Custom delay before remove (seconds)
  TEST_REMOVE_DELAY=10 python test_emoji_api.py
"""

import hashlib
import json
import os
import sys
import time

import requests

API_HOST = os.environ.get("INFOFLOW_API_HOST", "http://apiin.im.baidu.com")
APP_KEY = os.environ.get("INFOFLOW_APP_KEY", "")
APP_SECRET = os.environ.get("INFOFLOW_APP_SECRET", "")

# Defaults: group chat
DEFAULT_GROUP_ID = os.environ.get("INFOFLOW_TEST_GROUP", "4507088")
DEFAULT_MSG_ID = os.environ.get("TEST_BASE_MSG_ID", "1865794273048386548")
DEFAULT_MSG_ID2 = os.environ.get("TEST_MSG_ID2", "300014580")

# DM defaults
DEFAULT_DM_MSG_ID = os.environ.get("TEST_DM_MSG_ID", "")
DEFAULT_FROM_UID = os.environ.get("TEST_FROM_UID", "chengbo05")

REMOVE_DELAY = int(os.environ.get("TEST_REMOVE_DELAY", "0"))

# Emoji: 敲键盘
EMOJI_CONTENT = "d135"
EMOJI_DESC = "(qjp)"


def get_token():
    """Get app_access_token using app_key + md5(app_secret)."""
    md5_secret = hashlib.md5(APP_SECRET.encode("utf-8")).hexdigest().lower()
    url = f"{API_HOST}/api/v1/auth/app_access_token"
    resp = requests.post(url, json={"app_key": APP_KEY, "app_secret": md5_secret}, timeout=10)
    data = resp.json()
    token = data.get("data", {}).get("app_access_token") if isinstance(data.get("data"), dict) else None
    if not token:
        print(f"Token failed: {json.dumps(data, ensure_ascii=False)}")
        sys.exit(1)
    print(f"Token: {token[:20]}...")
    return token


def _headers(token):
    return {
        "Authorization": f"Bearer-{token}",
        "Content-Type": "application/json; charset=utf-8",
    }


def emoji_add(token, *, chat_type, chat_id, base_msg_id, msg_id2="", reply_content, reply_desc, from_uid=""):
    """Add emoji reaction to a message."""
    url = f"{API_HOST}/api/v1/im/message/emoji/add"
    payload = {"chatType": chat_type, "baseMsgId": base_msg_id}
    if chat_id:
        payload["chatId"] = chat_id
    if msg_id2:
        payload["msgId2"] = msg_id2
    if from_uid:
        payload["fromUid"] = from_uid
    if reply_content:
        payload["replyContent"] = reply_content
    if reply_desc:
        payload["replyDesc"] = reply_desc
    print(f"[ADD] POST {url}")
    print(f"[ADD] Body: {json.dumps(payload, ensure_ascii=False)}")
    resp = requests.post(url, json=payload, headers=_headers(token), timeout=10)
    print(f"[ADD] Response: {resp.text}")
    return resp.json()


def emoji_del(token, *, chat_type, chat_id, base_msg_id, msg_id2="", reply_content, from_uid=""):
    """Delete emoji reaction from a message."""
    url = f"{API_HOST}/api/v1/im/message/emoji/del"
    payload = {"chatType": chat_type, "baseMsgId": base_msg_id}
    if chat_id:
        payload["chatId"] = chat_id
    if msg_id2:
        payload["msgId2"] = msg_id2
    if from_uid:
        payload["fromUid"] = from_uid
    if reply_content:
        payload["replyContent"] = reply_content
    print(f"[DEL] POST {url}")
    print(f"[DEL] Body: {json.dumps(payload, ensure_ascii=False)}")
    resp = requests.post(url, json=payload, headers=_headers(token), timeout=10)
    print(f"[DEL] Response: {resp.text}")
    return resp.json()


def run_group(token):
    """Group chat test: chatType=2, chatId required, fromUid optional."""
    print("=" * 50)
    print("GROUP: chatType=2")
    print("=" * 50)
    emoji_add(token, chat_type=2, chat_id=int(DEFAULT_GROUP_ID),
              base_msg_id=DEFAULT_MSG_ID, msg_id2=DEFAULT_MSG_ID2,
              reply_content=EMOJI_CONTENT, reply_desc=EMOJI_DESC)
    if REMOVE_DELAY:
        print(f"Waiting {REMOVE_DELAY}s before remove...")
        time.sleep(REMOVE_DELAY)
    emoji_del(token, chat_type=2, chat_id=int(DEFAULT_GROUP_ID),
              base_msg_id=DEFAULT_MSG_ID, reply_content=EMOJI_CONTENT)


def run_dm(token, base_msg_id):
    """DM test: chatType=7, fromUid (user uuapName) required, no chatId."""
    print("=" * 50)
    print("DM: chatType=7")
    print("=" * 50)
    emoji_add(token, chat_type=7, chat_id=None,
              base_msg_id=base_msg_id,
              reply_content=EMOJI_CONTENT, reply_desc=EMOJI_DESC,
              from_uid=DEFAULT_FROM_UID)
    if REMOVE_DELAY:
        print(f"Waiting {REMOVE_DELAY}s before remove...")
        time.sleep(REMOVE_DELAY)
    emoji_del(token, chat_type=7, chat_id=None,
              base_msg_id=base_msg_id,
              reply_content=EMOJI_CONTENT,
              from_uid=DEFAULT_FROM_UID)


if __name__ == "__main__":
    chat_type = os.environ.get("TEST_CHAT_TYPE", "group").lower()

    token = get_token()
    print(f"Emoji: {EMOJI_CONTENT} ({EMOJI_DESC})")

    if chat_type == "dm":
        msg_id = DEFAULT_DM_MSG_ID or sys.exit("Set TEST_DM_MSG_ID for DM test")
        run_dm(token, msg_id)
    else:
        run_group(token)
