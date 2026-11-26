"""LLMLingua-2 prose compressor for ink (CPU only, no VRAM). Runs from .venv-compress, started on demand by ink.
POST /compress {"text": "...", "rate": 0.45} -> {"text": "...", "orig": n_tokens, "comp": n_tokens}
"""
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from llmlingua import PromptCompressor

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18620
MODEL = "microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank"
comp = PromptCompressor(model_name=MODEL, use_llmlingua2=True, device_map="cpu")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(200, {"status": "ok"})

    def do_POST(self):
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            r = comp.compress_prompt(req["text"], rate=float(req.get("rate", 0.45)),
                                     force_tokens=["\n", "?", "!", ".", ":", "-"], drop_consecutive=True)
            self._send(200, {"text": r["compressed_prompt"], "orig": r["origin_tokens"], "comp": r["compressed_tokens"]})
        except Exception as e:
            self._send(500, {"error": repr(e)})


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
