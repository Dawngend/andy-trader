import telegram_setup


SAMPLE_UPDATES = {
    "ok": True,
    "result": [
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 111}, "text": "hi"}},
        {"update_id": 2, "message": {"message_id": 2, "chat": {"id": 222}, "text": "hello again"}},
    ],
}

EMPTY_UPDATES = {"ok": True, "result": []}


def test_find_chat_id_uses_newest_message():
    calls = []

    def opener(url, data=None):
        calls.append(url)
        return SAMPLE_UPDATES

    assert telegram_setup.find_chat_id("tok", opener) == "222"


def test_find_chat_id_no_messages():
    def opener(url, data=None):
        return EMPTY_UPDATES

    assert telegram_setup.find_chat_id("tok", opener) is None


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

    result = telegram_setup.setup(opener=opener, setx=setx, token="super-secret-token")
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
