"""Local preview: serves web/ plus a mock of the Supabase REST endpoint.

Usage: python3 scripts/preview.py [rows.json] [port]
The frontend's config.js should point SUPABASE_URL at http://localhost:<port>.
"""
from __future__ import annotations

import json
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Handler(SimpleHTTPRequestHandler):
    rows: list[dict] = []

    def do_GET(self):
        if self.path.startswith("/rest/v1/events"):
            body = json.dumps(self.rows).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def main() -> None:
    rows_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mock_rows.json"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080

    Handler.rows = json.loads(Path(rows_path).read_text())
    handler = partial(Handler, directory=str(ROOT / "web"))
    print(f"Serving web/ + mock events API on http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()


if __name__ == "__main__":
    main()
