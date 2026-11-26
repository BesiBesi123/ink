"""Control Ink from your phone through your own Telegram bot - a live client of the ink engine (`ink --rpc`).

Setup (once):
  1. In Telegram, talk to @BotFather -> /newbot -> copy the token it gives you.
  2. Put it in ~/.ink/config.json:   "telegram_token": "123456:ABC..."
  3. Run `ink-telegram` (or: python ink_telegram.py). It prints a pairing code.
  4. Send your bot:  /pair <code>   - from then on only your chat is obeyed (saved as telegram_chat_id).

In the chat: any message is a task for Ink (the session continues). While it works you see one live progress
message (tools it runs, plan); approvals (push, deploy, writes outside the project) come as buttons.
  /new  fresh conversation     /cwd <dir>  project folder     /status  folder, model, busy
  /stop interrupt the task     /rewind     undo the last turn's file changes
  /steer <text>  guide the running task      anything starting with / else is passed to ink (/context, /map ...)
"""
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

from notify import CFG_FILE, config, send_telegram, telegram

APP = Path(__file__).resolve().parent


def save_config(**kv):
    cfg = config()
    cfg.update(kv)
    CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CFG_FILE.write_text(json.dumps(cfg, indent=2), "utf-8")


class Engine:
    """One `ink --rpc` process for a project folder; turns its events into Telegram messages."""
    def __init__(self, chat, cwd):
        self.chat, self.cwd = chat, cwd
        self.p = subprocess.Popen([sys.executable, "-X", "utf8", str(APP / "ink.py"), "--rpc"], cwd=cwd,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=open(Path.home() / ".ink" / "telegram-engine.log", "ab"),
                                  text=True, encoding="utf-8", bufsize=1)
        self.busy, self.lock = False, threading.Lock()
        self.reset_turn()
        self.info = {}
        threading.Thread(target=self.reader, daemon=True).start()
        threading.Thread(target=self.ticker, daemon=True).start()

    def reset_turn(self):
        self.text, self.lines, self.plan, self.msg_id, self.t0, self.dirty = [], [], [], None, time.time(), False

    def send(self, **m):
        try:
            self.p.stdin.write(json.dumps(m) + "\n")
            self.p.stdin.flush()
        except Exception:
            pass

    def alive(self):
        return self.p.poll() is None

    def progress_text(self):
        el = int(time.time() - self.t0)
        parts = [f"⏳ working… {el}s"]
        if self.plan:
            parts.append("\n".join(("✔ " if s["status"] == "done" else "▶ " if s["status"] == "doing" else
                                    "✘ " if s["status"] == "dropped" else "☐ ") + s["step"] for s in self.plan[:12]))
        if self.lines:
            parts.append("\n".join(self.lines[-12:]))
        return "\n\n".join(parts)[:3900]

    def ticker(self):
        """Edit the one progress message every few seconds (Telegram rate limits: don't edit per event)."""
        while self.alive():
            time.sleep(4)
            if not (self.busy and self.dirty):
                continue
            self.dirty = False
            try:
                if self.msg_id is None:
                    self.msg_id = telegram("sendMessage", chat_id=str(self.chat), text=self.progress_text())["message_id"]
                else:
                    telegram("editMessageText", chat_id=str(self.chat), message_id=self.msg_id, text=self.progress_text())
                telegram("sendChatAction", chat_id=str(self.chat), action="typing")
            except Exception:
                pass

    def reader(self):
        for line in self.p.stdout:
            try:
                e = json.loads(line)
            except Exception:
                continue
            t = e.get("type")
            if t == "hello":
                self.info = e
            elif t == "turn_start":
                self.busy = True
                self.reset_turn()
            elif t == "tool_start":
                self.lines.append("● " + e.get("label", ""))
                self.dirty = True
            elif t == "tool_end":
                if e.get("summary"):
                    self.lines.append(("   ⎿ " if e.get("ok") else "   ✗ ") + e["summary"][:120])
                self.dirty = True
            elif t == "plan":
                self.plan = e.get("steps") or []
                self.dirty = True
            elif t == "text_delta":
                self.text.append(e.get("text", ""))
            elif t == "notice" and e.get("level") in ("warn", "error"):
                self.lines.append("⚠ " + e.get("text", "")[:150])
                self.dirty = True
            elif t == "output":
                send_telegram(e.get("text", "")[:3900] or "(no output)", self.chat)
            elif t == "ask":
                kb = {"inline_keyboard": [[{"text": "✅ Yes", "callback_data": f"ask:{e['id']}:yes"},
                                           {"text": "♾ Always", "callback_data": f"ask:{e['id']}:always"},
                                           {"text": "⛔ No", "callback_data": f"ask:{e['id']}:no"}]]}
                try:
                    telegram("sendMessage", chat_id=str(self.chat), reply_markup=kb,
                             text=f"❓ {e.get('title', 'Allow?')}\n\n{e.get('detail', '')[:3000]}")
                except Exception:
                    pass
            elif t == "turn_end":
                was_turn = self.busy
                self.busy = False
                answer = "".join(self.text).strip()
                if self.msg_id is not None:
                    try:
                        telegram("editMessageText", chat_id=str(self.chat), message_id=self.msg_id,
                                 text=f"✔ done in {int(e.get('secs', 0))}s · {e.get('steps', 0)} steps"
                                 + ("\n\n" + "\n".join(self.lines[-8:]) if self.lines else ""))
                    except Exception:
                        pass
                if answer:
                    send_telegram(answer, self.chat)
                elif was_turn and e.get("interrupted"):
                    send_telegram("⏹ stopped.", self.chat)
                self.reset_turn()
        send_telegram("ink engine stopped (see ~/.ink/telegram-engine.log). Send a message to restart it.", self.chat)

    def close(self):
        self.send(type="quit")
        try:
            self.p.wait(30)
        except Exception:
            self.p.kill()


def main():
    cfg = config()
    if not cfg.get("telegram_token"):
        print(__doc__)
        sys.exit("telegram_token is not set in " + str(CFG_FILE))
    me = telegram("getMe")
    print(f"Ink is listening as @{me['username']}")
    code = None
    if not cfg.get("telegram_chat_id"):
        code = f"{secrets.randbelow(10**6):06d}"
        print(f"Pair your chat: send  /pair {code}  to @{me['username']}")
    cwd = cfg.get("telegram_cwd") or str(Path.home())
    eng = None
    offset = None
    while True:
        try:
            updates = telegram("getUpdates", _http_timeout=70, timeout=50, offset=offset)
        except Exception as e:
            print("telegram:", e)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            owner = config().get("telegram_chat_id")
            cq = u.get("callback_query")
            if cq:  # an approval button
                chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
                try:
                    telegram("answerCallbackQuery", callback_query_id=cq["id"])
                except Exception:
                    pass
                if owner and str(chat) == str(owner) and eng and str(cq.get("data", "")).startswith("ask:"):
                    _, aid, choice = cq["data"].split(":", 2)
                    eng.send(type="answer", id=aid, choice=choice)
                    send_telegram({"yes": "✅ allowed", "always": "♾ allowed for this session", "no": "⛔ refused"}[choice], chat)
                continue
            msg = u.get("message") or {}
            chat, text = (msg.get("chat") or {}).get("id"), (msg.get("text") or "").strip()
            if not chat or not text:
                continue
            if not owner:
                if code and text == f"/pair {code}":
                    save_config(telegram_chat_id=chat)
                    code = None
                    send_telegram("Paired. Send me a task. /new starts a fresh conversation.", chat)
                    print(f"paired with chat {chat}")
                continue
            if str(chat) != str(owner):
                continue  # strangers are ignored silently
            if eng is None or not eng.alive():
                eng = Engine(chat, cwd)
            cmd, _, arg = text.partition(" ")
            if cmd == "/start":
                send_telegram("Ink here. Send me a task.", chat)
            elif cmd == "/new":
                eng.send(type="command", name="clear", arg="")
                send_telegram("New conversation. What's next?", chat)
            elif cmd == "/cwd":
                p = Path(arg.strip()).expanduser()
                if p.is_dir():
                    cwd = str(p.resolve())
                    save_config(telegram_cwd=cwd)
                    eng.close()
                    eng = Engine(chat, cwd)
                    send_telegram(f"Folder: {cwd}", chat)
                else:
                    send_telegram(f"Not a folder: {arg}", chat)
            elif cmd == "/status":
                i = eng.info
                send_telegram(f"Folder: {cwd}\nModel: {i.get('model', '?')} ({i.get('effort', '')})\n"
                              f"Busy: {'yes' if eng.busy else 'no'}", chat)
            elif cmd == "/stop":
                eng.send(type="interrupt")
            elif cmd == "/steer":
                eng.send(type="steer", text=arg)
            elif cmd == "/rewind":
                eng.send(type="command", name="rewind", arg=arg)
            elif cmd.startswith("/"):
                eng.send(type="command", name=cmd[1:], arg=arg)
            else:
                if eng.busy:
                    send_telegram("Queued - I'll start it when the current task is done. (/steer <text> guides the "
                                  "running task instead.)", chat)
                eng.send(type="prompt", text=text)


if __name__ == "__main__":
    main()
