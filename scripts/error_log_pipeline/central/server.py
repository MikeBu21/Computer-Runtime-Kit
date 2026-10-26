#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Receive weekly Pi error packages and serve a small viewer (stdlib only).

Stores under RECEIVE_ROOT/logs/pi/YYYY-MM-DD/
Default listen: 0.0.0.0:9123
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

RECEIVE_ROOT = Path(
    os.environ.get(
        "ERROR_LOG_RECEIVE_ROOT",
        str(Path(__file__).resolve().parent / "data"),
    )
)
HOST = os.environ.get("ERROR_LOG_BIND", "0.0.0.0")
PORT = int(os.environ.get("ERROR_LOG_PORT", "9123"))
PI_ROOT = RECEIVE_ROOT / "logs" / "pi"


def list_weeks() -> List[str]:
    if not PI_ROOT.is_dir():
        return []
    weeks = [p.name for p in PI_ROOT.iterdir() if p.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}", p.name)]
    return sorted(weeks, reverse=True)


def load_meta(week: str) -> Optional[Dict[str, Any]]:
    d = PI_ROOT / week
    metas = list(d.glob("*.meta.json"))
    if not metas:
        return None
    try:
        return json.loads(metas[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_events(week: str, limit: int = 500) -> List[Dict[str, Any]]:
    d = PI_ROOT / week
    events: List[Dict[str, Any]] = []
    # prefer uncompressed if present, else gunzip
    jsonls = list(d.glob("*.jsonl"))
    if jsonls:
        text = jsonls[0].read_text(encoding="utf-8", errors="replace")
    else:
        import gzip

        gzs = list(d.glob("*.jsonl.gz"))
        if not gzs:
            return []
        with gzip.open(gzs[0], "rt", encoding="utf-8", errors="replace") as f:
            text = f.read()
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(events) >= limit:
            break
    return events


def page_index(weeks: List[str]) -> str:
    rows = []
    for w in weeks:
        meta = load_meta(w) or {}
        n = meta.get("event_count", "?")
        top = meta.get("top_errors") or []
        top1 = html.escape((top[0].get("sample") or "")[:80]) if top else "—"
        c1 = top[0].get("count") if top else ""
        rows.append(
            f"<tr><td><a href='/week?d={html.escape(w)}'>{html.escape(w)}</a></td>"
            f"<td>{html.escape(str(n))}</td>"
            f"<td>{c1}× {top1}</td></tr>"
        )
    body = "\n".join(rows) or "<tr><td colspan=3>Noch keine Wochenberichte.</td></tr>"
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><title>Pi Fehlerlogs</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;background:#0f1419;color:#e7ecf1}}
a{{color:#6cb6ff}} table{{border-collapse:collapse;width:100%;max-width:960px}}
th,td{{border-bottom:1px solid #2a3440;padding:.5rem .6rem;text-align:left;vertical-align:top}}
th{{color:#9fb0c0}} h1{{font-size:1.4rem}} .muted{{color:#8b9aab}}
</style></head><body>
<h1>piMiner – wöchentliche Fehlerberichte</h1>
<p class="muted">Empfang unter <code>{html.escape(str(PI_ROOT))}</code></p>
<table><thead><tr><th>Woche (So-Datum)</th><th>Einträge</th><th>Häufigster Fehler</th></tr></thead>
<tbody>{body}</tbody></table>
</body></html>"""


def page_week(week: str) -> str:
    meta = load_meta(week) or {}
    top = meta.get("top_errors") or []
    top_rows = []
    for t in top:
        top_rows.append(
            f"<tr><td><strong>{t.get('count')}</strong></td>"
            f"<td><code>{html.escape(str(t.get('fingerprint')))}</code></td>"
            f"<td><pre>{html.escape(str(t.get('sample') or ''))}</pre></td></tr>"
        )
    events = load_events(week)
    ev_rows = []
    for ev in events:
        ev_rows.append(
            f"<tr><td>{html.escape(str(ev.get('ts') or ''))}</td>"
            f"<td>{html.escape(str(ev.get('source') or ''))}</td>"
            f"<td><pre>{html.escape(str(ev.get('message') or '')[:2000])}</pre></td></tr>"
        )
    soft = meta.get("software") or {}
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><title>Fehler {html.escape(week)}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;background:#0f1419;color:#e7ecf1}}
a{{color:#6cb6ff}} table{{border-collapse:collapse;width:100%}}
th,td{{border-bottom:1px solid #2a3440;padding:.45rem .55rem;text-align:left;vertical-align:top}}
pre{{white-space:pre-wrap;margin:0;font-size:.85rem}}
.badge{{display:inline-block;background:#3d2a14;color:#ffb74d;padding:.15rem .45rem;border-radius:4px}}
h2{{margin-top:2rem;font-size:1.1rem}} .muted{{color:#8b9aab}}
</style></head><body>
<p><a href="/">← Alle Wochen</a></p>
<h1>Woche {html.escape(week)}</h1>
<p class="muted">Host {html.escape(str(meta.get('host') or '—'))} ·
Events {html.escape(str(meta.get('event_count') or len(events)))} ·
Git {html.escape(str(soft.get('git_sha') or '—'))}</p>
<p class="muted">{html.escape(str(meta.get('window_start') or ''))} → {html.escape(str(meta.get('window_end') or ''))}</p>
<h2>Häufigste Fehler</h2>
<table><thead><tr><th>Count</th><th>Fingerprint</th><th>Beispiel</th></tr></thead>
<tbody>{''.join(top_rows) or '<tr><td colspan=3>keine</td></tr>'}</tbody></table>
<h2>Einträge <span class="badge">max 500</span></h2>
<table><thead><tr><th>Zeit</th><th>Quelle</th><th>Meldung</th></tr></thead>
<tbody>{''.join(ev_rows) or '<tr><td colspan=3>leer</td></tr>'}</tbody></table>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            body = page_index(list_weeks()).encode("utf-8")
            self._send(200, body)
            return
        if parsed.path == "/week":
            qs = parse_qs(parsed.query)
            week = (qs.get("d") or [""])[0]
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", week):
                self._send(400, b"bad week")
                return
            body = page_week(week).encode("utf-8")
            self._send(200, body)
            return
        if parsed.path == "/health":
            self._send(200, b'{"ok":true}', "application/json")
            return
        self._send(404, b"not found")

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/upload":
            self._send(404, b"not found")
            return
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or "multipart/form-data" not in ctype:
            self._send(400, b"expected multipart")
            return
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            self._send(400, b"missing boundary")
            return
        boundary = m.group(1).strip().strip('"').encode()
        raw = self.rfile.read(length)
        files = _parse_multipart(raw, boundary)
        if "meta" not in files or "package" not in files:
            self._send(400, b"missing meta/package")
            return
        meta_name, meta_bytes = files["meta"]
        pkg_name, pkg_bytes = files["package"]
        try:
            meta = json.loads(meta_bytes.decode("utf-8"))
        except json.JSONDecodeError:
            self._send(400, b"bad meta json")
            return
        week = str(meta.get("week_label") or "unknown")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", week):
            week = "unknown"
        dest = PI_ROOT / week
        dest.mkdir(parents=True, exist_ok=True)
        meta_name = meta_name or f"piMiner-errors-{week}.meta.json"
        pkg_name = pkg_name or f"piMiner-errors-{week}.jsonl.gz"
        (dest / Path(meta_name).name).write_bytes(meta_bytes)
        (dest / Path(pkg_name).name).write_bytes(pkg_bytes)
        if pkg_name.endswith(".gz"):
            import gzip

            try:
                raw_jsonl = gzip.decompress(pkg_bytes)
                (dest / Path(pkg_name).name.replace(".jsonl.gz", ".jsonl")).write_bytes(raw_jsonl)
            except OSError:
                pass
        self._send(200, b'{"ok":true}', "application/json")


def _parse_multipart(body: bytes, boundary: bytes) -> Dict[str, Tuple[str, bytes]]:
    """Minimal multipart parser → {field: (filename, content)}."""
    out: Dict[str, Tuple[str, bytes]] = {}
    sep = b"--" + boundary
    for part in body.split(sep):
        if not part or part in (b"--", b"--\r\n", b"\r\n"):
            continue
        if part.startswith(b"--"):
            break
        if part.startswith(b"\r\n"):
            part = part[2:]
        if b"\r\n\r\n" not in part:
            continue
        header_blob, content = part.split(b"\r\n\r\n", 1)
        if content.endswith(b"\r\n"):
            content = content[:-2]
        headers = header_blob.decode("utf-8", errors="replace")
        name_m = re.search(r'name="([^"]+)"', headers)
        if not name_m:
            continue
        name = name_m.group(1)
        fn_m = re.search(r'filename="([^"]*)"', headers)
        filename = fn_m.group(1) if fn_m else ""
        out[name] = (filename, content)
    return out


def main() -> int:
    PI_ROOT.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"error-log viewer http://127.0.0.1:{PORT}/  receive_root={RECEIVE_ROOT}")
    print(f"upload POST http://<host>:{PORT}/upload")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
