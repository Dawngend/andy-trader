import subprocess

import pytest

import telegram_setup


SAMPLE_UPDATES = {
    "ok": True,
    "result": [
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 111},
                                      "from": {"username": "stranger"}, "text": "hi"}},
        {"update_id": 2, "message": {"message_id": 2, "chat": {"id": 222},
                                      "from": {"first_name": "Dawn"}, "text": "hello again"}},
    ],
}

EMPTY_UPDATES = {"ok": True, "result": []}

NON_MESSAGE_UPDATES = {
    "ok": True,
    "result": [
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 111},
                                      "from": {"first_name": "Dawn"}, "text": "hi"}},
        {"update_id": 2, "my_chat_member": {"chat": {"id": 222}}},
        {"update_id": 3, "edited_message": {"chat": {"id": 222}}},
        {"update_id": 4, "channel_post": {"chat": {"id": 333}}},
    ],
}


def test_find_chat_id_uses_newest_message():
    def opener(url, data=None):
        return SAMPLE_UPDATES

    message = telegram_setup.find_chat_id("tok", opener)
    assert str(message["chat"]["id"]) == "222"


def test_find_chat_id_no_messages():
    def opener(url, data=None):
        return EMPTY_UPDATES

    assert telegram_setup.find_chat_id("tok", opener) is None


def test_find_chat_id_skips_non_message_updates():
    def opener(url, data=None):
        return NON_MESSAGE_UPDATES

    message = telegram_setup.find_chat_id("tok", opener)
    assert str(message["chat"]["id"]) == "111"


def test_setup_reports_when_no_messages(capsys):
    def opener(url, data=None):
        return EMPTY_UPDATES

    result = telegram_setup.setup(opener=opener, setx=lambda *a: None, token="tok")
    assert result == 1
    assert "Send any message to your bot in Telegram first, then run this again." in capsys.readouterr().out


def test_setup_saves_both_vars_and_sends_test_message(capsys):
    setx_calls = []
    opener_calls = []

    def opener(url, data=None):
        opener_calls.append((url, data))
        return SAMPLE_UPDATES

    def setx(name, value):
        setx_calls.append((name, value))

    result = telegram_setup.setup(opener=opener, setx=setx, token="super-secret-token",
                                   prompt=lambda _: "y")
    assert result == 0
    names = [name for name, _ in setx_calls]
    assert names == ["ANDY_TELEGRAM_TOKEN", "ANDY_TELEGRAM_CHAT_ID"]
    assert setx_calls[0][1] == "super-secret-token"
    assert setx_calls[1][1] == "222"
    assert len(opener_calls) == 2
    assert opener_calls[1][1] is not None

    out = capsys.readouterr().out
    assert "super-secret-token" not in out
    assert "Restart the scheduled tasks" in out


def test_setup_strips_pasted_token():
    setx_calls = []

    def opener(url, data=None):
        return SAMPLE_UPDATES

    telegram_setup.setup(opener=opener, setx=lambda name, value: setx_calls.append((name, value)),
                          token="  super-secret-token  \n", prompt=lambda _: "y")
    assert setx_calls[0][1] == "super-secret-token"


def test_setup_asks_for_confirmation_before_saving(capsys):
    def opener(url, data=None):
        return SAMPLE_UPDATES

    prompts = []

    def prompt(question):
        prompts.append(question)
        return "y"

    telegram_setup.setup(opener=opener, setx=lambda *a: None, token="tok", prompt=prompt)
    assert len(prompts) == 1
    assert "Dawn" in prompts[0]
    assert "Is this you? (y/n)" in prompts[0]


def test_setup_saves_nothing_when_not_confirmed(capsys):
    setx_calls = []

    def opener(url, data=None):
        return SAMPLE_UPDATES

    result = telegram_setup.setup(opener=opener, setx=lambda n, v: setx_calls.append((n, v)),
                                   token="tok", prompt=lambda _: "n")
    assert result == 1
    assert setx_calls == []
    assert "Not confirmed, nothing saved." in capsys.readouterr().out


def test_setup_treats_anything_but_y_as_not_confirmed():
    setx_calls = []

    def opener(url, data=None):
        return SAMPLE_UPDATES

    result = telegram_setup.setup(opener=opener, setx=lambda n, v: setx_calls.append((n, v)),
                                   token="tok", prompt=lambda _: "")
    assert result == 1
    assert setx_calls == []


def test_default_setx_wraps_called_process_error():
    def raising_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["setx", "ANDY_TELEGRAM_TOKEN", "super-secret-token"],
                                             output=b"", stderr=b"super-secret-token")

    import telegram_setup as module
    original = module.subprocess.run
    module.subprocess.run = raising_run
    try:
        with pytest.raises(RuntimeError) as excinfo:
            module.default_setx("ANDY_TELEGRAM_TOKEN", "super-secret-token")
    finally:
        module.subprocess.run = original
    assert str(excinfo.value) == "setx failed for ANDY_TELEGRAM_TOKEN"
    assert "super-secret-token" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
