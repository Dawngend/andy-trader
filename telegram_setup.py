"""One-time helper: wire up Telegram phone alerts for trade_watch.py. Run it yourself."""

from __future__ import annotations

import getpass
import json
import subprocess
import urllib.parse
import urllib.request


def default_opener(url, data=None):
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def default_setx(name, value):
    try:
        subprocess.run(["setx", name, value], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        raise RuntimeError("setx failed for " + name) from None


def find_chat_id(token, opener):
    reply = opener(f"https://api.telegram.org/bot{token}/getUpdates")
    results = reply.get("result") or []
    for update in reversed(results):
        message = update.get("message")
        if message is not None:
            return message
    return None


def describe_sender(message):
    chat = message["chat"]
    sender = message.get("from") or {}
    who = sender.get("username") or sender.get("first_name") or "someone"
    title = chat.get("title") or chat.get("first_name")
    return who, title


def send_test_message(token, chat_id, opener):
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": "Andy Trader alerts are on"}).encode("utf-8")
    opener(f"https://api.telegram.org/bot{token}/sendMessage", body)


def setup(opener=default_opener, setx=default_setx, token=None, prompt=input):
    token = (token or getpass.getpass("Paste your Telegram bot token (from @BotFather): ")).strip()
    message = find_chat_id(token, opener)
    if message is None:
        print("Send any message to your bot in Telegram first, then run this again.")
        return 1
    chat_id = str(message["chat"]["id"])
    who, title = describe_sender(message)
    where = f' in "{title}"' if title else ""
    answer = prompt(f"Most recent message{where} is from {who}. Is this you? (y/n): ")
    if answer.strip().lower() != "y":
        print("Not confirmed, nothing saved.")
        return 1
    setx("ANDY_TELEGRAM_TOKEN", token)
    setx("ANDY_TELEGRAM_CHAT_ID", chat_id)
    send_test_message(token, chat_id, opener)
    print("Saved ANDY_TELEGRAM_TOKEN and ANDY_TELEGRAM_CHAT_ID as user environment variables.")
    print("Restart the scheduled tasks, or sign out and back in, so they pick up the new variables.")
    return 0


def main():
    return setup()


if __name__ == "__main__":
    raise SystemExit(main())
