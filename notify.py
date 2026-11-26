"""Send the user a message: Telegram when configured (~/.ink/config.json telegram_token + telegram_chat_id),
otherwise a Windows notification. Every alert is also appended to ~/.ink/alerts.log.

usage: python notify.py "message"
"""
import datetime
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

STATE = Path.home() / ".ink"
CFG_FILE = STATE / "config.json"


def config() -> dict:
    try:
        return json.loads(CFG_FILE.read_text("utf-8"))
    except Exception:
        return {}


def telegram(method: str, _http_timeout: int = 30, **params):
    token = os.environ.get("INK_TELEGRAM_TOKEN") or config().get("telegram_token")
    if not token:
        raise RuntimeError("no telegram_token in ~/.ink/config.json")
    data = urllib.parse.urlencode({k: v if isinstance(v, str) else json.dumps(v)
                                   for k, v in params.items() if v is not None}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=_http_timeout) as r:
        body = json.loads(r.read())
    if not body.get("ok"):
        raise RuntimeError(body.get("description") or str(body))
    return body["result"]


def send_telegram(text: str, chat_id=None):
    chat = chat_id or config().get("telegram_chat_id")
    if not chat:
        raise RuntimeError("no telegram_chat_id in ~/.ink/config.json")
    for i in range(0, max(len(text), 1), 4000):  # Telegram's limit is 4096 chars per message
        telegram("sendMessage", chat_id=str(chat), text=text[i:i + 4000] or "(empty)")


def toast(title: str, text: str):
    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "''")
    ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
          "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
          "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
          f"$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text>{esc(title)}</text><text>{esc(text[:300])}</text></binding></visual></toast>');"
          "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, timeout=30)


def notify(text: str, title: str = "Ink") -> str:
    """Deliver an alert; returns how it was delivered."""
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "alerts.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {title}: {text}\n")
    try:
        send_telegram(f"{title}\n{text}")
        return "telegram"
    except Exception:
        pass
    try:
        toast(title, text)
        return "windows notification"
    except Exception:
        return "log only"


if __name__ == "__main__":
    print(notify(" ".join(sys.argv[1:]) or "test from Ink"))
