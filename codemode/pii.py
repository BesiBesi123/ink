"""Mask personal data (emails, phone numbers, names) as [EMAIL_1]/[PHONE_1]/[NAME_1] before it reaches the model.

The placeholder -> value map lives in ~/.ink/pii.json, outside the sandbox, so code the model writes can never
read it. Placeholders are mapped back to real values only when they are sent to a tool (see sandbox.Proxy).
"""
import json
import re
import threading
from pathlib import Path

VAULT_FILE = Path.home() / ".ink" / "pii.json"

EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
# +385 91 234 5678, (555) 123-4567, 555.123.4567, +44 20 7946 0958. Needs a leading + or a separator between digit
# groups and 9-15 digits in all, so ids, timestamps, versions and dates don't match.
PHONE = re.compile(r"(?<![\w+])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{1,4}\)[\s.-]?)?\d{2,4}(?:[\s.-]\d{2,4}){1,4}(?![\w])"
                   r"|(?<![\w+])\+\d{9,15}(?!\w)")
DATE_LIKE = re.compile(r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}$|^\d{1,2}[-./]\d{1,2}[-./]\d{2,4}$|^\d{1,3}(\.\d{1,3}){3}$")
NAME_KEYS = re.compile(r"^(name|full_?name|first_?name|last_?name|given_?name|family_?name|surname|display_?name|"
                       r"contact(_?name)?|author|owner_?name|customer(_?name)?|recipient(_?name)?|sender(_?name)?)$", re.I)
PLACEHOLDER = re.compile(r"\[(EMAIL|PHONE|NAME)_\d+\]")


def _phone_ok(s: str) -> bool:
    digits = sum(c.isdigit() for c in s)
    return 9 <= digits <= 15 and not DATE_LIKE.match(s.strip())


class Vault:
    def __init__(self, path: Path = VAULT_FILE):
        self.path = path
        self.lock = threading.Lock()
        self.by_value: dict[str, str] = {}
        self.by_token: dict[str, str] = {}
        try:
            self.by_token = json.loads(path.read_text("utf-8"))
            self.by_value = {v: k for k, v in self.by_token.items()}
        except Exception:
            pass

    def _token(self, kind: str, value: str) -> str:
        with self.lock:
            tok = self.by_value.get(value)
            if tok:
                return tok
            n = 1 + sum(t.startswith(f"[{kind}_") for t in self.by_token)
            tok = f"[{kind}_{n}]"
            self.by_value[value], self.by_token[tok] = tok, value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.by_token), "utf-8")
            return tok

    def mask_text(self, s: str) -> str:
        s = EMAIL.sub(lambda m: self._token("EMAIL", m.group(0)), s)
        return PHONE.sub(lambda m: self._token("PHONE", m.group(0)) if _phone_ok(m.group(0)) else m.group(0), s)

    def mask(self, obj, key: str = ""):
        """Mask a tool result: strings, and dicts/lists of them. Values under name-like keys become [NAME_n]."""
        if isinstance(obj, str):
            if key and NAME_KEYS.match(key) and obj.strip() and not PLACEHOLDER.fullmatch(obj.strip()):
                return self._token("NAME", obj)
            return self.mask_text(obj)
        if isinstance(obj, dict):
            return {k: self.mask(v, str(k)) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.mask(v, key) for v in obj]
        return obj

    def unmask(self, obj):
        """Map placeholders back to real values (only ever done on the way into a tool)."""
        if isinstance(obj, str):
            return PLACEHOLDER.sub(lambda m: self.by_token.get(m.group(0), m.group(0)), obj)
        if isinstance(obj, dict):
            return {k: self.unmask(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.unmask(v) for v in obj]
        return obj
