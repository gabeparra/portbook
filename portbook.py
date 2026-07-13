#!/usr/bin/env python3
"""portbook — a dead-simple LOCAL tracker for your machines, their ports, and tickets.

Run:  python3 portbook.py
It serves a small visual UI on http://127.0.0.1:8099 (loopback ONLY — never the network)
and saves everything to portbook.json next to this file. No pip, no Docker, no server to
deploy, no login. Ctrl+C to stop. Back up your data with `cp portbook.json ...` or git.

The stylesheet is Tailwind, compiled to plain CSS and inlined below so this stays a single
self-contained offline file. To restyle: edit build/input.css and run build/rebuild.sh.
"""
import argparse
import datetime
import hmac
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    """Minimal .env reader — KEY=VALUE lines, # comments, optional surrounding quotes."""
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env

SEED = {
    "schema": 1,
    "machines": [
        {"name": "computer1", "role": "", "host": "", "os": "", "location": "",
         "status": "active", "notes": "", "ports": []},
        {"name": "computer2", "role": "", "host": "", "os": "", "location": "",
         "status": "active", "notes": "", "ports": []},
    ],
    "tickets": [],
}


def load_state(path):
    """Read the data file, seeding it on first run. Never silently wipes on a parse error."""
    if not os.path.exists(path):
        save_state(json.loads(json.dumps(SEED)), path)
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stderr.write(
            f"\n  !! {path} is not valid JSON (line {e.lineno}, column {e.colno}): {e.msg}\n"
            f"     Fix it by hand, or restore {os.path.basename(path)}.bak.\n"
            f"     Refusing to load so your data stays safe.\n\n")
        raise
    state.setdefault("machines", [])
    state.setdefault("tickets", [])
    return state


def save_state(state, path):
    """Atomic write (tmp + os.replace) and keep the prior good copy as .bak (one-step undo)."""
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass
    state["updated"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".portbook.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class Handler(BaseHTTPRequestHandler):
    server_version = "portbook"

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _cookies(self):
        jar = {}
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                jar[k] = v
        return jar

    def _authed(self):
        if not self.server.password:
            return True  # no password configured → auth disabled
        tok = self._cookies().get("pb_session", "")
        return bool(tok) and tok in self.server.sessions

    def _redirect(self, loc):
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_login(self):
        if not self.server.password:
            self._send(200, json.dumps({"ok": True}))
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            pw = str(json.loads(self.rfile.read(length) or b"{}").get("password", ""))
        except (ValueError, json.JSONDecodeError):
            pw = ""
        if hmac.compare_digest(pw, self.server.password):
            tok = secrets.token_urlsafe(24)
            self.server.sessions.add(tok)
            self._send(200, json.dumps({"ok": True}), extra={
                "Set-Cookie": f"pb_session={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"})
        else:
            self._send(401, json.dumps({"error": "wrong password"}))

    def _handle_logout(self):
        self.server.sessions.discard(self._cookies().get("pb_session", ""))
        self._send(200, json.dumps({"ok": True}), extra={
            "Set-Cookie": "pb_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/login":
            self._redirect("/") if self._authed() else self._send(200, LOGIN_HTML, "text/html; charset=utf-8")
            return
        if path == "/":
            self._send(200, HTML if self._authed() else LOGIN_HTML, "text/html; charset=utf-8")
            return
        if not self._authed():
            self._send(401, '{"error":"auth required"}')
            return
        if path == "/api/state":
            self._send(200, json.dumps(load_state(self.server.data_path)))
        elif path == "/api/info":
            self._send(200, json.dumps({"path": self.server.data_path, "auth": bool(self.server.password)}))
        elif path == "/api/backup":
            state = load_state(self.server.data_path)
            fn = "portbook-" + datetime.date.today().isoformat() + ".json"
            self._send(200, json.dumps(state, indent=2, ensure_ascii=False),
                       extra={"Content-Disposition": f'attachment; filename="{fn}"'})
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if self.path == "/api/login":
            self._handle_login()
            return
        if self.path == "/api/logout":
            self._handle_logout()
            return
        if self.path != "/api/state":
            self._send(404, '{"error":"not found"}')
            return
        if not self._authed():
            self._send(401, '{"error":"auth required"}')
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            state = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(state.get("machines"), list):
                raise ValueError("'machines' must be a list")
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, json.dumps({"error": str(e)}))
            return
        save_state(state, self.server.data_path)
        self._send(200, json.dumps({"ok": True, "updated": state.get("updated")}))

    def log_message(self, *args):
        pass  # keep the terminal quiet


def main():
    ap = argparse.ArgumentParser(description="Local machine / port / ticket tracker.")
    ap.add_argument("--port", type=int, default=8099, help="port (default 8099)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1 / loopback)")
    ap.add_argument("--file", default=os.path.join(HERE, "portbook.json"), help="data file")
    ap.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    args = ap.parse_args()

    env = load_env(os.path.join(HERE, ".env"))
    password = (os.environ.get("PORTBOOK_PASSWORD") or env.get("PORTBOOK_PASSWORD") or "").strip()

    path = os.path.abspath(args.file)
    load_state(path)  # seed-if-missing / fail loudly on bad JSON before we start

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.data_path = path
    httpd.password = password
    httpd.sessions = set()

    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    url = f"http://{'127.0.0.1' if loopback else args.host}:{args.port}/"
    print(f"\n  portbook   →  {url}")
    print(f"  data file  →  {path}")
    print(f"  auth       →  {'ON, password required' if password else 'off — set PORTBOOK_PASSWORD in .env to enable'}")
    if loopback:
        print("  loopback only — not reachable from any network. Ctrl+C to stop.\n")
    else:
        print(f"  reachable on the network at {args.host}. Ctrl+C to stop.")
        if not password:
            print("  !! WARNING: bound to a network interface with NO password — anyone who can reach\n"
                  "     this host can read and change your data. Set PORTBOOK_PASSWORD in .env.\n")
        else:
            print("  note: plain HTTP — use only over a trusted network (LAN / VPN / tailnet).\n")
    if not args.no_open and loopback:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print(f"\n  stopped. your data is in {path}\n")


# ---------------------------------------------------------------------------- UI
HTML = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>portbook</title>
<style>/* tw:start */
/*! tailwindcss v4.3.2 | MIT License | https://tailwindcss.com */
@layer properties{@supports (((-webkit-hyphens:none)) and (not (margin-trim:inline))) or ((-moz-orient:inline) and (not (color:rgb(from red r g b)))){*,:before,:after,::backdrop{--tw-rotate-x:initial;--tw-rotate-y:initial;--tw-rotate-z:initial;--tw-skew-x:initial;--tw-skew-y:initial;--tw-border-style:solid;--tw-font-weight:initial;--tw-tracking:initial;--tw-backdrop-blur:initial;--tw-backdrop-brightness:initial;--tw-backdrop-contrast:initial;--tw-backdrop-grayscale:initial;--tw-backdrop-hue-rotate:initial;--tw-backdrop-invert:initial;--tw-backdrop-opacity:initial;--tw-backdrop-saturate:initial;--tw-backdrop-sepia:initial;--tw-leading:initial;--tw-shadow:0 0 #0000;--tw-shadow-color:initial;--tw-shadow-alpha:100%;--tw-inset-shadow:0 0 #0000;--tw-inset-shadow-color:initial;--tw-inset-shadow-alpha:100%;--tw-ring-color:initial;--tw-ring-shadow:0 0 #0000;--tw-inset-ring-color:initial;--tw-inset-ring-shadow:0 0 #0000;--tw-ring-inset:initial;--tw-ring-offset-width:0px;--tw-ring-offset-color:#fff;--tw-ring-offset-shadow:0 0 #0000;--tw-content:""}}}@layer theme{:root,:host{--font-sans:system-ui, -apple-system, "Segoe UI", Roboto, Inter, sans-serif;--font-mono:ui-monospace, "SF Mono", "JetBrains Mono", "Cascadia Code", Menlo, Consolas, monospace;--spacing:.25rem;--text-xs:.75rem;--text-xs--line-height:calc(1 / .75);--text-sm:.875rem;--text-sm--line-height:calc(1.25 / .875);--text-lg:1.125rem;--text-lg--line-height:calc(1.75 / 1.125);--font-weight-medium:500;--font-weight-semibold:600;--font-weight-bold:700;--tracking-wider:.05em;--leading-relaxed:1.625;--radius-md:.375rem;--radius-lg:.5rem;--radius-2xl:1rem;--default-transition-duration:.15s;--default-transition-timing-function:cubic-bezier(.4, 0, .2, 1);--default-font-family:var(--font-sans);--default-mono-font-family:var(--font-mono);--color-bg:#0f0f11;--color-panel:#17171b;--color-panel2:#1e1e23;--color-line:#2b2b32;--color-line2:#3d3d46;--color-ink:#f3f4f6;--color-dim:#a6acb8;--color-faint:#6d7079;--color-amber:#f2b544;--color-up:#46c98a;--color-wip:#e6a13a;--color-paused:#7c7c85;--color-down:#ec6a6a;--color-topen:#a3aab8;--color-twip:#e6a13a;--color-tblocked:#ec6a6a;--color-tdone:#46c98a}}@layer base{*,:after,:before,::backdrop{box-sizing:border-box;border:0 solid;margin:0;padding:0}::file-selector-button{box-sizing:border-box;border:0 solid;margin:0;padding:0}html,:host{-webkit-text-size-adjust:100%;tab-size:4;line-height:1.5;font-family:var(--default-font-family,ui-sans-serif, system-ui, sans-serif, "Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol", "Noto Color Emoji");font-feature-settings:var(--default-font-feature-settings,normal);font-variation-settings:var(--default-font-variation-settings,normal);-webkit-tap-highlight-color:transparent}hr{height:0;color:inherit;border-top-width:1px}abbr:where([title]){-webkit-text-decoration:underline dotted;text-decoration:underline dotted}h1,h2,h3,h4,h5,h6{font-size:inherit;font-weight:inherit}a{color:inherit;-webkit-text-decoration:inherit;-webkit-text-decoration:inherit;-webkit-text-decoration:inherit;text-decoration:inherit}b,strong{font-weight:bolder}code,kbd,samp,pre{font-family:var(--default-mono-font-family,ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace);font-feature-settings:var(--default-mono-font-feature-settings,normal);font-variation-settings:var(--default-mono-font-variation-settings,normal);font-size:1em}small{font-size:80%}sub,sup{vertical-align:baseline;font-size:75%;line-height:0;position:relative}sub{bottom:-.25em}sup{top:-.5em}table{text-indent:0;border-color:inherit;border-collapse:collapse}:-moz-focusring{outline:auto}progress{vertical-align:baseline}summary{display:list-item}ol,ul,menu{list-style:none}img,svg,video,canvas,audio,iframe,embed,object{vertical-align:middle;display:block}img,video{max-width:100%;height:auto}button,input,select,optgroup,textarea{font:inherit;font-feature-settings:inherit;font-variation-settings:inherit;letter-spacing:inherit;color:inherit;opacity:1;background-color:#0000;border-radius:0}::file-selector-button{font:inherit;font-feature-settings:inherit;font-variation-settings:inherit;letter-spacing:inherit;color:inherit;opacity:1;background-color:#0000;border-radius:0}:where(select:is([multiple],[size])) optgroup{font-weight:bolder}:where(select:is([multiple],[size])) optgroup option{padding-inline-start:20px}::file-selector-button{margin-inline-end:4px}::placeholder{opacity:1}@supports (not ((-webkit-appearance:-apple-pay-button))) or (contain-intrinsic-size:1px){::placeholder{color:currentColor}@supports (color:color-mix(in lab, red, red)){::placeholder{color:color-mix(in oklab, currentcolor 50%, transparent)}}}textarea{resize:vertical}::-webkit-search-decoration{-webkit-appearance:none}::-webkit-date-and-time-value{min-height:1lh;text-align:inherit}::-webkit-datetime-edit{display:inline-flex}::-webkit-datetime-edit-fields-wrapper{padding:0}::-webkit-datetime-edit{padding-block:0}::-webkit-datetime-edit-year-field{padding-block:0}::-webkit-datetime-edit-month-field{padding-block:0}::-webkit-datetime-edit-day-field{padding-block:0}::-webkit-datetime-edit-hour-field{padding-block:0}::-webkit-datetime-edit-minute-field{padding-block:0}::-webkit-datetime-edit-second-field{padding-block:0}::-webkit-datetime-edit-millisecond-field{padding-block:0}::-webkit-datetime-edit-meridiem-field{padding-block:0}::-webkit-calendar-picker-indicator{line-height:1}:-moz-ui-invalid{box-shadow:none}button,input:where([type=button],[type=reset],[type=submit]){appearance:button}::file-selector-button{appearance:button}::-webkit-inner-spin-button{height:auto}::-webkit-outer-spin-button{height:auto}[hidden]:where(:not([hidden=until-found])){display:none!important}html{font-size:15px}body{background-color:var(--color-bg);font-family:var(--font-sans);--tw-leading:var(--leading-relaxed);line-height:var(--leading-relaxed);color:var(--color-ink);-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}input,select,textarea{min-width:0;max-width:100%}::selection{background:#f2b54447}@supports (color:color-mix(in lab, red, red)){::selection{background:color-mix(in srgb, var(--color-amber) 28%, transparent)}}}@layer components{.field{border-radius:var(--radius-lg);border-style:var(--tw-border-style);border-width:1px;border-color:var(--color-line);background-color:var(--color-panel2);padding-inline:calc(var(--spacing) * 2.5);padding-block:calc(var(--spacing) * 1.5);color:var(--color-ink);transition-property:color,background-color,border-color,outline-color,text-decoration-color,fill,stroke,--tw-gradient-from,--tw-gradient-via,--tw-gradient-to,opacity,box-shadow,transform,translate,scale,rotate,filter,-webkit-backdrop-filter,backdrop-filter,display,content-visibility,overlay,pointer-events;transition-timing-function:var(--tw-ease,var(--default-transition-timing-function));transition-duration:var(--tw-duration,var(--default-transition-duration));--tw-outline-style:none;outline-style:none}.field:focus{border-color:var(--color-amber);--tw-ring-shadow:var(--tw-ring-inset,) 0 0 0 calc(2px + var(--tw-ring-offset-width)) var(--tw-ring-color,currentcolor);box-shadow:var(--tw-inset-shadow), var(--tw-inset-ring-shadow), var(--tw-ring-offset-shadow), var(--tw-ring-shadow), var(--tw-shadow);--tw-ring-color:#f2b54440}@supports (color:color-mix(in lab, red, red)){.field:focus{--tw-ring-color:color-mix(in oklab, var(--color-amber) 25%, transparent)}}.flat{border-style:var(--tw-border-style);border-width:0;border-bottom-style:var(--tw-border-style);padding-inline:0;padding-block:var(--spacing);color:var(--color-ink);transition-property:color,background-color,border-color,outline-color,text-decoration-color,fill,stroke,--tw-gradient-from,--tw-gradient-via,--tw-gradient-to,opacity,box-shadow,transform,translate,scale,rotate,filter,-webkit-backdrop-filter,backdrop-filter,display,content-visibility,overlay,pointer-events;transition-timing-function:var(--tw-ease,var(--default-transition-timing-function));transition-duration:var(--tw-duration,var(--default-transition-duration));--tw-outline-style:none;background-color:#0000;border-color:#0000;border-bottom-width:1px;border-radius:0;outline-style:none}.flat:focus{border-color:var(--color-line2)}.btn{cursor:pointer;border-radius:var(--radius-lg);border-style:var(--tw-border-style);border-width:1px;border-color:var(--color-line);padding-inline:calc(var(--spacing) * 3);padding-block:calc(var(--spacing) * 1.5);font-family:var(--font-mono);font-size:var(--text-xs);line-height:var(--tw-leading,var(--text-xs--line-height));color:var(--color-dim);transition-property:color,background-color,border-color,outline-color,text-decoration-color,fill,stroke,--tw-gradient-from,--tw-gradient-via,--tw-gradient-to,opacity,box-shadow,transform,translate,scale,rotate,filter,-webkit-backdrop-filter,backdrop-filter,display,content-visibility,overlay,pointer-events;transition-timing-function:var(--tw-ease,var(--default-transition-timing-function));transition-duration:var(--tw-duration,var(--default-transition-duration));background-color:#0000}@media (hover:hover){.btn:hover{border-color:#f2b544b3}@supports (color:color-mix(in lab, red, red)){.btn:hover{border-color:color-mix(in oklab, var(--color-amber) 70%, transparent)}}.btn:hover{color:var(--color-ink)}}.eyebrow{margin-top:calc(var(--spacing) * 4);margin-bottom:calc(var(--spacing) * 2);align-items:center;gap:calc(var(--spacing) * 2);font-family:var(--font-mono);--tw-tracking:.14em;letter-spacing:.14em;color:var(--color-faint);text-transform:uppercase;font-size:.7rem;display:flex}.eyebrow:after{content:var(--tw-content);content:var(--tw-content);content:var(--tw-content);background-color:var(--color-line);--tw-content:"";content:var(--tw-content);flex:1;height:1px}.addrow{cursor:pointer;border-radius:var(--radius-lg);border-style:var(--tw-border-style);--tw-border-style:dashed;border-style:dashed;border-width:1px;border-color:var(--color-line);width:100%;padding-inline:calc(var(--spacing) * 3);padding-block:calc(var(--spacing) * 1.5);text-align:left;font-family:var(--font-mono);color:var(--color-faint);transition-property:color,background-color,border-color,outline-color,text-decoration-color,fill,stroke,--tw-gradient-from,--tw-gradient-via,--tw-gradient-to,opacity,box-shadow,transform,translate,scale,rotate,filter,-webkit-backdrop-filter,backdrop-filter,display,content-visibility,overlay,pointer-events;transition-timing-function:var(--tw-ease,var(--default-transition-timing-function));transition-duration:var(--tw-duration,var(--default-transition-duration));background-color:#0000;font-size:.82rem}@media (hover:hover){.addrow:hover{border-color:var(--color-amber);color:var(--color-amber)}}.led{height:calc(var(--spacing) * 2.5);width:calc(var(--spacing) * 2.5);border-radius:3.40282e38px;flex-shrink:0}.tab{cursor:pointer;border-style:var(--tw-border-style);border-width:0;border-bottom-style:var(--tw-border-style);padding-inline:calc(var(--spacing) * 2.5);padding-block:calc(var(--spacing) * 1.5);font-family:var(--font-mono);font-size:var(--text-sm);line-height:var(--tw-leading,var(--text-sm--line-height));color:var(--color-dim);transition-property:color,background-color,border-color,outline-color,text-decoration-color,fill,stroke,--tw-gradient-from,--tw-gradient-via,--tw-gradient-to,opacity,box-shadow,transform,translate,scale,rotate,filter,-webkit-backdrop-filter,backdrop-filter,display,content-visibility,overlay,pointer-events;transition-timing-function:var(--tw-ease,var(--default-transition-timing-function));transition-duration:var(--tw-duration,var(--default-transition-duration));background-color:#0000;border-color:#0000;border-bottom-width:2px}@media (hover:hover){.tab:hover{color:var(--color-ink)}}.tab.on{border-color:var(--color-amber);color:var(--color-ink)}.card{border-radius:var(--radius-2xl);border-style:var(--tw-border-style);border-width:1px;border-color:var(--color-line);background-color:var(--color-panel);padding:calc(var(--spacing) * 5);--tw-shadow:0 12px 30px -18px var(--tw-shadow-color,#000000bf);box-shadow:var(--tw-inset-shadow), var(--tw-inset-ring-shadow), var(--tw-ring-offset-shadow), var(--tw-ring-shadow), var(--tw-shadow);transition-property:color,background-color,border-color,outline-color,text-decoration-color,fill,stroke,--tw-gradient-from,--tw-gradient-via,--tw-gradient-to;transition-timing-function:var(--tw-ease,var(--default-transition-timing-function));transition-duration:var(--tw-duration,var(--default-transition-duration));position:relative}@media (hover:hover){.card:hover{border-color:var(--color-line2)}}.stsel{cursor:pointer;border-style:var(--tw-border-style);border-width:1px;border-color:var(--color-line);background-color:var(--color-panel2);padding-inline:calc(var(--spacing) * 2.5);padding-block:var(--spacing);font-family:var(--font-mono);color:var(--color-dim);border-radius:3.40282e38px;font-size:.72rem}.portnum{border-radius:var(--radius-md);border-style:var(--tw-border-style);border-width:1px;border-color:var(--color-line);background-color:var(--color-panel2);width:4.6rem;padding-inline:calc(var(--spacing) * 2);padding-block:calc(var(--spacing) * 1.5);text-align:right;font-family:var(--font-mono);color:var(--color-amber)}.proto{border-radius:var(--radius-md);border-style:var(--tw-border-style);border-width:1px;border-color:var(--color-line);padding-inline:var(--spacing);padding-block:calc(var(--spacing) * 1.5);font-family:var(--font-mono);color:var(--color-dim);text-transform:uppercase;background-color:#0000;font-size:.78rem}.iconbtn{cursor:pointer;border-style:var(--tw-border-style);padding:0;padding-inline:var(--spacing);--tw-leading:1;color:var(--color-faint);background-color:#0000;border-width:0;line-height:1}@media (hover:hover){.iconbtn:hover{color:var(--color-down)}}.tk{border-radius:var(--radius-lg);border-style:var(--tw-border-style);border-width:1px;border-color:var(--color-line);background-color:var(--color-panel2);overflow:hidden}.tksum{cursor:pointer;align-items:center;gap:calc(var(--spacing) * 2);padding-inline:calc(var(--spacing) * 2.5);padding-block:calc(var(--spacing) * 2);list-style-type:none;display:flex}.tdot{height:calc(var(--spacing) * 2.5);width:calc(var(--spacing) * 2.5);border-radius:3.40282e38px;flex-shrink:0}.tkbody{gap:calc(var(--spacing) * 2);border-top-style:var(--tw-border-style);border-top-width:1px;border-color:var(--color-line);padding-inline:calc(var(--spacing) * 2.5);padding-top:calc(var(--spacing) * 2);padding-bottom:calc(var(--spacing) * 2.5);flex-direction:column;display:flex}.tcard{margin-bottom:calc(var(--spacing) * 2.5);border-radius:var(--radius-lg);border-style:var(--tw-border-style);border-width:1px;border-left-style:var(--tw-border-style);border-left-width:3px;border-color:var(--color-line);background-color:var(--color-panel2);padding:calc(var(--spacing) * 2.5)}.boardcol{border-radius:var(--radius-2xl);border-style:var(--tw-border-style);border-width:1px;border-color:var(--color-line);background-color:var(--color-panel);padding:calc(var(--spacing) * 3)}}@layer utilities{.invisible{visibility:hidden}.absolute{position:absolute}.sticky{position:sticky}.top-0{top:0}.top-3{top:calc(var(--spacing) * 3)}.right-3{right:calc(var(--spacing) * 3)}.z-10{z-index:10}.col-span-2{grid-column:span 2/span 2}.mx-auto{margin-inline:auto}.my-1{margin-block:var(--spacing)}.my-1\.5{margin-block:calc(var(--spacing) * 1.5)}.mt-0{margin-top:0}.mt-0\.5{margin-top:calc(var(--spacing) * .5)}.mt-1{margin-top:var(--spacing)}.mt-1\.5{margin-top:calc(var(--spacing) * 1.5)}.mt-2{margin-top:calc(var(--spacing) * 2)}.mt-3{margin-top:calc(var(--spacing) * 3)}.mb-2{margin-bottom:calc(var(--spacing) * 2)}.mb-2\.5{margin-bottom:calc(var(--spacing) * 2.5)}.ml-\[1\.2rem\]{margin-left:1.2rem}.ml-auto{margin-left:auto}.block{display:block}.flex{display:flex}.grid{display:grid}.hidden{display:none}.inline{display:inline}.table{display:table}.max-h-16{max-height:calc(var(--spacing) * 16)}.min-h-\[2\.6rem\]{min-height:2.6rem}.min-h-\[5\.5rem\]{min-height:5.5rem}.w-\[4\.6rem\]{width:4.6rem}.w-full{width:100%}.max-w-\[1180px\]{max-width:1180px}.min-w-0{min-width:0}.min-w-\[120px\]{min-width:120px}.min-w-\[170px\]{min-width:170px}.flex-1{flex:1}.shrink{flex-shrink:1}.shrink-0{flex-shrink:0}.border-collapse{border-collapse:collapse}.transform{transform:var(--tw-rotate-x,) var(--tw-rotate-y,) var(--tw-rotate-z,) var(--tw-skew-x,) var(--tw-skew-y,)}.cursor-pointer{cursor:pointer}.list-none{list-style-type:none}.grid-cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}.grid-cols-3{grid-template-columns:repeat(3,minmax(0,1fr))}.grid-cols-\[4\.6rem_3rem_1fr_auto\]{grid-template-columns:4.6rem 3rem 1fr auto}.grid-cols-\[4rem_1fr\]{grid-template-columns:4rem 1fr}.grid-cols-\[repeat\(auto-fill\,minmax\(340px\,1fr\)\)\]{grid-template-columns:repeat(auto-fill,minmax(340px,1fr))}.grid-cols-\[repeat\(auto-fit\,minmax\(240px\,1fr\)\)\]{grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}.flex-col{flex-direction:column}.flex-wrap{flex-wrap:wrap}.items-baseline{align-items:baseline}.items-center{align-items:center}.gap-1{gap:var(--spacing)}.gap-1\.5{gap:calc(var(--spacing) * 1.5)}.gap-2{gap:calc(var(--spacing) * 2)}.gap-3{gap:calc(var(--spacing) * 3)}.gap-4{gap:calc(var(--spacing) * 4)}.self-start{align-self:flex-start}.truncate{text-overflow:ellipsis;white-space:nowrap;overflow:hidden}.overflow-hidden{overflow:hidden}.rounded-md{border-radius:var(--radius-md)}.border{border-style:var(--tw-border-style);border-width:1px}.border-0{border-style:var(--tw-border-style);border-width:0}.border-b{border-bottom-style:var(--tw-border-style);border-bottom-width:1px}.border-line{border-color:var(--color-line)}.bg-amber{background-color:var(--color-amber)}.bg-bg{background-color:var(--color-bg)}.bg-bg\/85{background-color:#0f0f11d9}@supports (color:color-mix(in lab, red, red)){.bg-bg\/85{background-color:color-mix(in oklab, var(--color-bg) 85%, transparent)}}.bg-panel{background-color:var(--color-panel)}.bg-transparent{background-color:#0000}.p-5{padding:calc(var(--spacing) * 5)}.px-0{padding-inline:0}.px-1{padding-inline:var(--spacing)}.px-1\.5{padding-inline:calc(var(--spacing) * 1.5)}.px-2{padding-inline:calc(var(--spacing) * 2)}.px-2\.5{padding-inline:calc(var(--spacing) * 2.5)}.px-4{padding-inline:calc(var(--spacing) * 4)}.px-5{padding-inline:calc(var(--spacing) * 5)}.py-0{padding-block:0}.py-0\.5{padding-block:calc(var(--spacing) * .5)}.py-1{padding-block:var(--spacing)}.py-1\.5{padding-block:calc(var(--spacing) * 1.5)}.py-2{padding-block:calc(var(--spacing) * 2)}.py-2\.5{padding-block:calc(var(--spacing) * 2.5)}.py-12{padding-block:calc(var(--spacing) * 12)}.pt-2{padding-top:calc(var(--spacing) * 2)}.pr-2{padding-right:calc(var(--spacing) * 2)}.pb-10{padding-bottom:calc(var(--spacing) * 10)}.pl-\[1\.2rem\]{padding-left:1.2rem}.text-center{text-align:center}.text-left{text-align:left}.text-right{text-align:right}.font-mono{font-family:var(--font-mono)}.font-sans{font-family:var(--font-sans)}.text-lg{font-size:var(--text-lg);line-height:var(--tw-leading,var(--text-lg--line-height))}.text-\[0\.7rem\]{font-size:.7rem}.text-\[0\.8rem\]{font-size:.8rem}.text-\[0\.9rem\]{font-size:.9rem}.text-\[0\.72rem\]{font-size:.72rem}.text-\[0\.74rem\]{font-size:.74rem}.text-\[0\.76rem\]{font-size:.76rem}.text-\[0\.78rem\]{font-size:.78rem}.text-\[0\.82rem\]{font-size:.82rem}.text-\[0\.85rem\]{font-size:.85rem}.text-\[0\.92rem\]{font-size:.92rem}.text-\[1\.05rem\]{font-size:1.05rem}.font-bold{--tw-font-weight:var(--font-weight-bold);font-weight:var(--font-weight-bold)}.font-medium{--tw-font-weight:var(--font-weight-medium);font-weight:var(--font-weight-medium)}.font-semibold{--tw-font-weight:var(--font-weight-semibold);font-weight:var(--font-weight-semibold)}.tracking-wider{--tw-tracking:var(--tracking-wider);letter-spacing:var(--tracking-wider)}.whitespace-pre-wrap{white-space:pre-wrap}.text-amber{color:var(--color-amber)}.text-dim{color:var(--color-dim)}.text-down{color:var(--color-down)}.text-faint{color:var(--color-faint)}.text-ink{color:var(--color-ink)}.text-wip{color:var(--color-wip)}.uppercase{text-transform:uppercase}.italic{font-style:italic}.opacity-0{opacity:0}.backdrop-blur{--tw-backdrop-blur:blur(8px);-webkit-backdrop-filter:var(--tw-backdrop-blur,) var(--tw-backdrop-brightness,) var(--tw-backdrop-contrast,) var(--tw-backdrop-grayscale,) var(--tw-backdrop-hue-rotate,) var(--tw-backdrop-invert,) var(--tw-backdrop-opacity,) var(--tw-backdrop-saturate,) var(--tw-backdrop-sepia,);backdrop-filter:var(--tw-backdrop-blur,) var(--tw-backdrop-brightness,) var(--tw-backdrop-contrast,) var(--tw-backdrop-grayscale,) var(--tw-backdrop-hue-rotate,) var(--tw-backdrop-invert,) var(--tw-backdrop-opacity,) var(--tw-backdrop-saturate,) var(--tw-backdrop-sepia,)}.transition{transition-property:color,background-color,border-color,outline-color,text-decoration-color,fill,stroke,--tw-gradient-from,--tw-gradient-via,--tw-gradient-to,opacity,box-shadow,transform,translate,scale,rotate,filter,-webkit-backdrop-filter,backdrop-filter,display,content-visibility,overlay,pointer-events;transition-timing-function:var(--tw-ease,var(--default-transition-timing-function));transition-duration:var(--tw-duration,var(--default-transition-duration))}@media (hover:hover){.group-hover\:opacity-100:is(:where(.group):hover *){opacity:1}.hover\:text-amber:hover{color:var(--color-amber)}.hover\:text-down:hover{color:var(--color-down)}.hover\:underline:hover{text-decoration-line:underline}}}.led.up{background:var(--color-up);box-shadow:0 0 7px #46c98ab3}@supports (color:color-mix(in lab, red, red)){.led.up{box-shadow:0 0 7px color-mix(in srgb,var(--color-up) 70%,transparent)}}.led.wip{background:var(--color-wip);box-shadow:0 0 7px #e6a13ab3}@supports (color:color-mix(in lab, red, red)){.led.wip{box-shadow:0 0 7px color-mix(in srgb,var(--color-wip) 70%,transparent)}}.led.paused{background:var(--color-paused)}.led.offline{background:var(--color-down);box-shadow:0 0 7px #ec6a6ab3}@supports (color:color-mix(in lab, red, red)){.led.offline{box-shadow:0 0 7px color-mix(in srgb,var(--color-down) 70%,transparent)}}.tdot.open{background:var(--color-topen)}.tdot.wip{background:var(--color-twip)}.tdot.blocked{background:var(--color-tblocked)}.tdot.done{background:var(--color-tdone)}.tcard.open{border-left-color:var(--color-topen)}.tcard.wip{border-left-color:var(--color-twip)}.tcard.blocked{border-left-color:var(--color-tblocked)}.tcard.done{border-left-color:var(--color-tdone)}.tk>summary::-webkit-details-marker{display:none}.tk>summary:before{content:"▸";color:var(--color-faint);font-size:.72rem;transition:transform .15s}.tk[open]>summary:before{transform:rotate(90deg)}.twist>summary::-webkit-details-marker{display:none}.twist>summary:before{content:"+ ";color:var(--color-faint)}.twist[open]>summary:before{content:"−"}.port .num-spin::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}.port .num-spin::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}.port .num-spin{-moz-appearance:textfield}#netmap{background:var(--color-bg);border:1px solid var(--color-line);-webkit-user-select:none;user-select:none;border-radius:16px}.mnode{cursor:grab}.mnode-box{fill:var(--color-panel);stroke:var(--color-line);stroke-width:1.5px;transition:stroke .15s}.mnode:hover .mnode-box{stroke:var(--color-amber)}.mnode-icon rect,.mnode-icon line{stroke:var(--color-amber);stroke-width:2px;stroke-linecap:round}.mnode-name{fill:var(--color-ink);font:600 13px/1 var(--font-mono)}.mnode-sub{fill:var(--color-faint);font:11px/1 var(--font-mono)}.mnode-led.up{fill:var(--color-up)}.mnode-led.wip{fill:var(--color-wip)}.mnode-led.paused{fill:var(--color-paused)}.mnode-led.offline{fill:var(--color-down)}.medge line{stroke:var(--color-line2);stroke-width:1.5px}.medge-lblbg{fill:var(--color-panel2);stroke:var(--color-line)}.medge-lbl{fill:var(--color-dim);font:11px/1 var(--font-mono)}#arrow path{fill:var(--color-line2)}@media (prefers-reduced-motion:reduce){*{transition:none!important}}@media print{header,.statline{position:static}.tab,.btn,#q,#status,.addrow,.iconbtn,.tkdel,.delcard{display:none!important}.card,.boardcol{break-inside:avoid;box-shadow:none}body{color:#000;background:#fff}}@property --tw-rotate-x{syntax:"*";inherits:false}@property --tw-rotate-y{syntax:"*";inherits:false}@property --tw-rotate-z{syntax:"*";inherits:false}@property --tw-skew-x{syntax:"*";inherits:false}@property --tw-skew-y{syntax:"*";inherits:false}@property --tw-border-style{syntax:"*";inherits:false;initial-value:solid}@property --tw-font-weight{syntax:"*";inherits:false}@property --tw-tracking{syntax:"*";inherits:false}@property --tw-backdrop-blur{syntax:"*";inherits:false}@property --tw-backdrop-brightness{syntax:"*";inherits:false}@property --tw-backdrop-contrast{syntax:"*";inherits:false}@property --tw-backdrop-grayscale{syntax:"*";inherits:false}@property --tw-backdrop-hue-rotate{syntax:"*";inherits:false}@property --tw-backdrop-invert{syntax:"*";inherits:false}@property --tw-backdrop-opacity{syntax:"*";inherits:false}@property --tw-backdrop-saturate{syntax:"*";inherits:false}@property --tw-backdrop-sepia{syntax:"*";inherits:false}@property --tw-leading{syntax:"*";inherits:false}@property --tw-shadow{syntax:"*";inherits:false;initial-value:0 0 #0000}@property --tw-shadow-color{syntax:"*";inherits:false}@property --tw-shadow-alpha{syntax:"<percentage>";inherits:false;initial-value:100%}@property --tw-inset-shadow{syntax:"*";inherits:false;initial-value:0 0 #0000}@property --tw-inset-shadow-color{syntax:"*";inherits:false}@property --tw-inset-shadow-alpha{syntax:"<percentage>";inherits:false;initial-value:100%}@property --tw-ring-color{syntax:"*";inherits:false}@property --tw-ring-shadow{syntax:"*";inherits:false;initial-value:0 0 #0000}@property --tw-inset-ring-color{syntax:"*";inherits:false}@property --tw-inset-ring-shadow{syntax:"*";inherits:false;initial-value:0 0 #0000}@property --tw-ring-inset{syntax:"*";inherits:false}@property --tw-ring-offset-width{syntax:"<length>";inherits:false;initial-value:0}@property --tw-ring-offset-color{syntax:"*";inherits:false;initial-value:#fff}@property --tw-ring-offset-shadow{syntax:"*";inherits:false;initial-value:0 0 #0000}@property --tw-content{syntax:"*";inherits:false;initial-value:""}
/* tw:end */</style>
</head><body>
<header class="sticky top-0 z-10 flex flex-wrap items-center gap-3 px-4 py-2.5 bg-bg/85 backdrop-blur border-b border-line">
  <div class="flex items-center gap-2 font-mono font-bold text-[1.05rem]"><span class="led bg-amber" style="box-shadow:0 0 9px var(--color-amber)"></span>portbook</div>
  <nav class="flex gap-1">
    <button class="tab on" data-tab="machines">machines</button>
    <button class="tab" data-tab="map">map</button>
    <button class="tab" data-tab="tickets">tickets</button>
    <button class="tab" data-tab="ports">ports</button>
  </nav>
  <input id="q" class="field flex-1 min-w-[170px] font-sans" placeholder="search…">
  <button class="btn" id="add">+ machine</button>
  <button class="btn" id="addtk">+ ticket</button>
  <a class="btn" href="api/backup">backup</a>
  <button class="btn" onclick="print()">print</button>
  <button class="btn hidden" id="logout">logout</button>
  <span id="status" class="font-mono text-[0.78rem] text-faint min-w-[120px] text-right"></span>
</header>
<div id="stat" class="statline font-mono text-[0.82rem] text-dim px-4 py-2 bg-panel border-b border-line flex gap-2 flex-wrap"></div>
<main id="app" class="p-5 max-w-[1180px] mx-auto"></main>
<footer id="foot" class="max-w-[1180px] mx-auto px-5 pt-2 pb-10 text-faint text-[0.78rem] font-mono"></footer>
<script>
const STAT=["active","wip","paused","offline"], TST=["open","wip","blocked","done"], PRIO=["low","med","high"];
const LED={active:"up",wip:"wip",paused:"paused",offline:"offline"};
const WK={20:"FTP data",21:"FTP",22:"SSH / SFTP",23:"Telnet",25:"SMTP (mail)",53:"DNS",67:"DHCP",68:"DHCP",
69:"TFTP",80:"HTTP (web)",110:"POP3",111:"RPC",123:"NTP",137:"NetBIOS",139:"NetBIOS / SMB",143:"IMAP",
161:"SNMP",162:"SNMP trap",179:"BGP",389:"LDAP",443:"HTTPS (web)",445:"SMB",465:"SMTPS",514:"syslog",
587:"SMTP submission",636:"LDAPS",873:"rsync",993:"IMAPS",995:"POP3S",1080:"SOCKS proxy",1194:"OpenVPN",
1433:"MS SQL Server",1521:"Oracle DB",1723:"PPTP VPN",2049:"NFS",2375:"Docker",2376:"Docker (TLS)",
3000:"dev / app server",3128:"proxy",3306:"MySQL / MariaDB",3389:"RDP (remote desktop)",5060:"SIP",
5432:"PostgreSQL",5601:"Kibana",5672:"AMQP / RabbitMQ",5900:"VNC",5985:"WinRM",6379:"Redis",
6443:"Kubernetes API",8000:"HTTP (alt)",8080:"HTTP (alt / proxy)",8443:"HTTPS (alt)",8888:"HTTP (alt)",
9000:"app / MinIO",9090:"Prometheus",9200:"Elasticsearch",11211:"Memcached",15672:"RabbitMQ UI",27017:"MongoDB"};
let S={machines:[]}, TAB="machines", Q="";
const NW=118,NH=86,HW=NW/2,HH=NH/2; let mapDrag=null;
function edgePoint(cx,cy,tx,ty){ const dx=tx-cx,dy=ty-cy; if(!dx&&!dy)return[cx,cy]; const s=Math.min(dx?HW/Math.abs(dx):Infinity, dy?HH/Math.abs(dy):Infinity); return [cx+dx*s,cy+dy*s]; }
const app=document.getElementById("app"),statusEl=document.getElementById("status"),footEl=document.getElementById("foot"),statEl=document.getElementById("stat");
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const opt=(v,cur)=>`<option ${v===cur?"selected":""}>${v}</option>`;

async function load(){
  let r; try{ r=await fetch("api/state"); }catch(e){ setStatus("can't reach portbook"); return; }
  if(r.status===401){ location.reload(); return; }
  try{ S=await r.json(); }catch(e){ S={machines:[]}; }
  if(!Array.isArray(S.machines)) S.machines=[];
  if(!Array.isArray(S.tickets)) S.tickets=[];
  // migrate old per-machine tickets → one top-level list linked by machine name
  let _mig=false;
  S.machines.forEach(m=>{
    if(Array.isArray(m.tickets)){ m.tickets.forEach(tk=>{ tk.machine=m.name; S.tickets.push(tk); _mig=true; }); delete m.tickets; }
    (m.ports||[]).forEach(p=>{ if(!Array.isArray(p.links)){ p.links = p.connectsTo ? [{to:p.connectsTo, detail:p.connects||""}] : []; _mig=true; } if(("connectsTo" in p)||("connects" in p)){ delete p.connectsTo; delete p.connects; _mig=true; } });
  });
  if(_mig) save();
  setStatus(S.updated?("saved "+fmt(S.updated)):"");
  try{ const i=await (await fetch("api/info")).json();
    footEl.innerHTML=`saved locally, as you type, to <code class="text-dim">${esc(i.path)}</code> — gitignored, on this machine only · prior copy kept as <code class="text-dim">.bak</code> · back up with <code class="text-dim">cp</code> or <code class="text-dim">git</code>`;
    if(i.auth){ const lo=document.getElementById("logout"); lo.classList.remove("hidden"); lo.onclick=async()=>{ try{await fetch("api/logout",{method:"POST"});}catch(_){} location.reload(); }; }
  }catch(e){}
  const h=location.hash.replace("#","");
  if(["machines","map","tickets","ports"].includes(h)) TAB=h;
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("on",x.dataset.tab===TAB));
  render();
}
let t; function save(){ setStatus("saving…"); clearTimeout(t);
  t=setTimeout(async()=>{ try{
      const r=await fetch("api/state",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(S)});
      if(r.status===401){ setStatus("session expired"); location.reload(); return; }
      const j=await r.json(); if(j.ok){S.updated=j.updated;setStatus("saved "+fmt(j.updated));}else setStatus("error: "+(j.error||"?"));
    }catch(e){ setStatus("save failed — is portbook running?"); } },250);
}
function setStatus(s){ statusEl.textContent=s; }
function fmt(iso){ if(!iso)return""; try{return new Date(iso).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"});}catch(e){return iso;} }
function statline(){
  let ports=0,openT=0; const pk={};
  S.machines.forEach(m=>(m.ports||[]).forEach(p=>{ports++;const k=(p.number||0)+"/"+(p.proto||"tcp");pk[k]=(pk[k]||0)+1;}));
  (S.tickets||[]).forEach(k=>{ if((k.status||"open")!=="done") openT++; });
  const reuse=Object.values(pk).filter(c=>c>1).length;
  statEl.innerHTML=`<span><b class="text-ink">${S.machines.length}</b> machines</span><span class="text-faint">·</span>`+
    `<span><b class="text-ink">${ports}</b> ports</span><span class="text-faint">·</span><span><b class="text-ink">${openT}</b> open ticket${openT===1?"":"s"}</span>`+
    (reuse?`<span class="text-faint">·</span><span class="text-wip">⚠ ${reuse} reused port${reuse>1?"s":""}</span>`:"");
}
function matchM(m){ if(!Q)return true; const q=Q.toLowerCase();
  return [m.name,m.role,m.host,m.os,m.location,m.notes,...(m.ports||[]).flatMap(p=>[p.number,p.proto,p.serves,...(p.links||[]).flatMap(l=>[l.to,l.detail])])].join(" ").toLowerCase().includes(q);
}
function matchT(k){ if(!Q)return true; const q=Q.toLowerCase();
  return [k.ref,k.title,k.status,k.priority,k.from,k.link,k.body,k.machine].join(" ").toLowerCase().includes(q);
}
function setF(i,k,v){ if(k==="name"){const old=S.machines[i].name;S.machines[i].name=v;(S.tickets||[]).forEach(t=>{if(t.machine===old)t.machine=v;});S.machines.forEach(mm=>(mm.ports||[]).forEach(p=>(p.links||[]).forEach(l=>{if(l.to===old)l.to=v;})));} else {S.machines[i][k]=v;} save();statline(); }
function setPort(i,j,k,v){S.machines[i].ports[j][k]=k==="number"?(parseInt(v,10)||0):v;save();statline();}
function setTk(ti,k,v){S.tickets[ti][k]=v;save();statline();}
function wk(i,j){const p=S.machines[i].ports[j];const g=WK[p.number];if(g&&!String(p.serves||"").trim()){p.serves=g;save();render();}}
function addM(){S.machines.push({name:"new machine",role:"",status:"active",host:"",os:"",location:"",notes:"",ports:[]});save();render();}
function delM(i){const nm=S.machines[i].name;if(confirm("Delete "+(nm||"this machine")+"?")){(S.tickets||[]).forEach(t=>{if(t.machine===nm)t.machine="";});S.machines.forEach(mm=>(mm.ports||[]).forEach(p=>{if(Array.isArray(p.links))p.links=p.links.filter(l=>l.to!==nm);}));S.machines.splice(i,1);save();render();}}
function addPort(i){S.machines[i].ports.push({number:0,proto:"tcp",serves:"",links:[]});save();render();}
function delPort(i,j){S.machines[i].ports.splice(j,1);save();render();}
function addLink(i,j){(S.machines[i].ports[j].links=S.machines[i].ports[j].links||[]).push({to:"",detail:""});save();render();}
function setLink(i,j,li,k,v){S.machines[i].ports[j].links[li][k]=v;save();if(k==="to")render();else statline();}
function delLink(i,j,li){S.machines[i].ports[j].links.splice(li,1);save();render();}
function addTk(machine){S.tickets.push({ref:"",title:"",status:"open",priority:"med",from:"",link:"",body:"",machine:machine||""});save();render();}
function addTkFor(i){addTk(S.machines[i].name);}
function newTicket(){TAB="machines";location.hash="machines";document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("on",x.dataset.tab==="machines"));addTk("");}
function delTk(ti){if(confirm("Delete this ticket?")){S.tickets.splice(ti,1);save();render();}}
function reflink(k){
  const url=(k.link||"").trim(), ok=/^https?:\/\//i.test(url);
  if(ok){ const t=(k.ref?("#"+esc(k.ref)):"open")+" ↗"; return `<a href="${esc(url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" class="font-mono text-[0.74rem] text-amber hover:underline shrink min-w-0 truncate" title="open the external ticket">${t}</a>`; }
  if(k.ref){ return `<span class="font-mono text-[0.74rem] text-faint shrink min-w-0 truncate">#${esc(k.ref)}</span>`; }
  return "";
}
function machineOpts(k){ return `<option value="" ${!k.machine?"selected":""}>— unassigned</option>`+S.machines.map(m=>`<option value="${esc(m.name)}" ${k.machine===m.name?"selected":""}>${esc(m.name)}</option>`).join(""); }
function connectOpts(l){ return `<option value="" ${!l.to?"selected":""}>— none</option>`+S.machines.map(m=>`<option value="${esc(m.name)}" ${l.to===m.name?"selected":""}>${esc(m.name)}</option>`).join("")+`<option value="external" ${l.to==="external"?"selected":""}>external</option>`; }
function incomingFor(name){ const r=[]; S.machines.forEach(m=>(m.ports||[]).forEach(p=>(p.links||[]).forEach(l=>{ if(l.to===name) r.push({p,from:m,detail:l.detail}); }))); return r; }
function ticketEditor(k,ti){ return `
        <details class="tk" ${(!k.title&&!k.body)?"open":""}>
          <summary class="tksum"><span class="tdot ${k.status||"open"}"></span><span class="flex-1 min-w-0 truncate text-[0.92rem]">${esc(k.title)|| "<span class='text-faint'>(new ticket — click to fill)</span>"}</span>${reflink(k)}</summary>
          <div class="tkbody">
            <div class="grid grid-cols-3 gap-1.5">
              <select class="field" onchange="setTk(${ti},'status',this.value)" title="status">${TST.map(v=>opt(v,k.status||"open")).join("")}</select>
              <select class="field" onchange="setTk(${ti},'priority',this.value)" title="priority">${PRIO.map(v=>opt(v,k.priority||"med")).join("")}</select>
              <input class="field" value="${esc(k.ref)}" oninput="setTk(${ti},'ref',this.value)" placeholder="their ref #">
            </div>
            <input class="field" value="${esc(k.title)}" oninput="setTk(${ti},'title',this.value)" placeholder="short title / what it's about">
            <input class="field" value="${esc(k.link)}" oninput="setTk(${ti},'link',this.value)" placeholder="link to the external ticket (https://…)">
            <input class="field" value="${esc(k.from)}" oninput="setTk(${ti},'from',this.value)" placeholder="from — the org that raised it (e.g. NetOps)">
            <label class="grid grid-cols-[4rem_1fr] items-center gap-2 font-mono text-[0.72rem] text-faint"><span>machine</span><select class="field font-sans" onchange="setTk(${ti},'machine',this.value)" title="which machine this ticket is about">${machineOpts(k)}</select></label>
            <textarea class="field font-mono text-[0.85rem] min-h-[5.5rem]" oninput="setTk(${ti},'body',this.value)" placeholder="notes / pasted ticket text (optional — mostly a reference)">${esc(k.body)}</textarea>
            <button class="tkdel self-start bg-transparent border-0 text-down text-[0.8rem] cursor-pointer" onclick="delTk(${ti})">delete ticket</button>
          </div>
        </details>`; }

function render(){ statline(); (TAB==="tickets"?renderTickets:TAB==="ports"?renderPorts:TAB==="map"?renderMap:renderMachines)(); }
function renderMachines(){
  const list=S.machines.map((m,i)=>({m,i})).filter(x=>matchM(x.m));
  const unassigned=S.tickets.map((k,ti)=>({k,ti})).filter(x=>!x.k.machine && matchT(x.k));
  if(!list.length && !unassigned.length){ app.innerHTML=`<div class="text-dim text-center py-12 font-mono">no machines${Q?" match “"+esc(Q)+"”":""} — click <b class="text-ink">+ machine</b></div>`; return; }
  const cards=list.map(({m,i})=>{ const inc=incomingFor(m.name); return `
    <div class="card group">
      <button class="delcard absolute top-3 right-3 bg-transparent border-0 text-faint hover:text-down cursor-pointer opacity-0 group-hover:opacity-100 transition" title="delete machine" onclick="delM(${i})">🗑</button>
      <div class="flex items-center gap-2">
        <span class="led ${LED[m.status||"active"]}"></span>
        <div class="flex-1 min-w-0"><input class="flat font-mono font-bold text-lg w-full" value="${esc(m.name)}" oninput="setF(${i},'name',this.value)" placeholder="hostname"></div>
        <select class="stsel" onchange="setF(${i},'status',this.value)">${STAT.map(v=>opt(v,m.status||"active")).join("")}</select>
      </div>
      <div class="mt-0.5"><input class="flat text-dim w-full" value="${esc(m.role)}" oninput="setF(${i},'role',this.value)" placeholder="what does it do?"></div>
      <div class="mt-1 flex items-center gap-2 font-mono text-[0.82rem] text-faint"><span class="shrink-0">host</span><input class="flat text-dim flex-1" value="${esc(m.host)}" oninput="setF(${i},'host',this.value)" placeholder="hostname / FQDN / IP (optional)"></div>
      <div class="eyebrow">ports</div>
      <div class="flex flex-col gap-2">${(m.ports||[]).map((p,j)=>`
        <div class="port flex flex-col gap-1">
          <div class="grid grid-cols-[4.6rem_3rem_1fr_auto] gap-1.5 items-center">
            <input class="portnum num-spin" type="number" min="1" max="65535" value="${p.number||""}" oninput="setPort(${i},${j},'number',this.value)" onchange="wk(${i},${j})" placeholder="port">
            <select class="proto" onchange="setPort(${i},${j},'proto',this.value)">${["tcp","udp"].map(v=>opt(v,p.proto||"tcp")).join("")}</select>
            <input class="flat" value="${esc(p.serves)}" oninput="setPort(${i},${j},'serves',this.value)" placeholder="${esc(WK[p.number]||"what it serves")}">
            <button class="iconbtn" title="remove" onclick="delPort(${i},${j})">✕</button>
          </div>
          ${(p.links||[]).map((l,li)=>`
          <div class="flex items-center gap-1.5 text-[0.82rem] pl-[1.2rem]">
            <span class="text-faint font-mono shrink-0">↳ to</span>
            <select class="field font-sans py-1 text-[0.82rem]" onchange="setLink(${i},${j},${li},'to',this.value)" title="connects to which machine">${connectOpts(l)}</select>
            <input class="flat text-dim flex-1" value="${esc(l.detail)}" oninput="setLink(${i},${j},${li},'detail',this.value)" placeholder="port / detail (optional)">
            <button class="iconbtn" title="remove connection" onclick="delLink(${i},${j},${li})">✕</button>
          </div>`).join("")}
          <button class="self-start ml-[1.2rem] font-mono text-[0.74rem] text-faint hover:text-amber bg-transparent border-0 cursor-pointer px-0 py-0.5" onclick="addLink(${i},${j})">+ connection</button>
        </div>`).join("")}
        <button class="addrow" onclick="addPort(${i})">+ port</button>
      </div>
      ${inc.length?`<div class="eyebrow">incoming</div>
      <div class="flex flex-col gap-1">${inc.map(({p,from,detail})=>`
        <div class="flex items-center gap-2 text-[0.85rem]" title="opened by a connection from ${esc(from.name)} — edit it on that machine">
          <span class="font-mono text-amber w-[4.6rem] text-right pr-2 shrink-0">${p.number||"?"}</span>
          <span class="font-mono text-dim text-[0.76rem] uppercase shrink-0">${esc(p.proto||"tcp")}</span>
          <span class="text-dim truncate min-w-0">← ${esc(from.name)}${from.host?` <span class="text-faint">(${esc(from.host)})</span>`:""}${detail?` <span class="text-faint">${esc(detail)}</span>`:""}</span>
        </div>`).join("")}
      </div>`:""}
      <div class="eyebrow">tickets</div>
      <div class="flex flex-col gap-1.5">${S.tickets.map((k,ti)=>({k,ti})).filter(x=>x.k.machine===m.name).map(({k,ti})=>ticketEditor(k,ti)).join("")}
        <button class="addrow" onclick="addTkFor(${i})">+ ticket</button>
      </div>
      <details class="twist mt-3">
        <summary class="text-faint cursor-pointer font-mono text-[0.72rem] list-none">os / location / notes</summary>
        <div class="grid grid-cols-2 gap-1.5 mt-2">
          <input class="field" value="${esc(m.os)}" oninput="setF(${i},'os',this.value)" placeholder="OS">
          <input class="field" value="${esc(m.location)}" oninput="setF(${i},'location',this.value)" placeholder="location">
          <textarea class="field col-span-2 min-h-[2.6rem]" oninput="setF(${i},'notes',this.value)" placeholder="notes">${esc(m.notes)}</textarea>
        </div>
      </details>
    </div>`; }).join("");
  const uCard=unassigned.length?`
    <div class="card">
      <div class="flex items-center gap-2"><span class="led paused"></span><div class="font-mono font-bold text-lg">unassigned tickets</div></div>
      <div class="mt-0.5 text-dim text-[0.9rem]">not tied to a machine yet — pick one in each ticket to connect it</div>
      <div class="eyebrow">tickets</div>
      <div class="flex flex-col gap-1.5">${unassigned.map(({k,ti})=>ticketEditor(k,ti)).join("")}
        <button class="addrow" onclick="addTk('')">+ ticket</button>
      </div>
    </div>`:"";
  app.innerHTML=`<div class="grid gap-4 grid-cols-[repeat(auto-fill,minmax(340px,1fr))]">`+cards+uCard+`</div>`;
}
function renderTickets(){
  const cols={open:[],wip:[],blocked:[],done:[]};
  S.tickets.map((k,ti)=>({k,ti})).forEach(({k,ti})=>{ if(!matchT(k))return; (cols[k.status]||cols.open).push({k,ti}); });
  const label={open:"open",wip:"in progress",blocked:"blocked",done:"done"};
  app.innerHTML=`<div class="grid gap-4 grid-cols-[repeat(auto-fit,minmax(240px,1fr))]">`+TST.map(st=>`
    <div class="boardcol">
      <div class="flex items-center gap-2 font-mono text-[0.82rem] mb-2.5 text-dim"><span class="tdot ${st}"></span> ${label[st]} <b class="text-ink">${cols[st].length}</b></div>
      ${cols[st].map(({k,ti})=>`
        <div class="tcard ${st}">
          <div class="flex gap-2 items-baseline"><b class="font-semibold flex-1 min-w-0 truncate">${esc(k.title)|| "<span class='text-faint'>(untitled)</span>"}</b> ${reflink(k)}</div>
          <div class="font-mono text-[0.72rem] text-faint mt-0.5">${k.machine?esc(k.machine):"<span class='text-faint'>unassigned</span>"}${k.priority?` · ${esc(k.priority)}`:""}${k.from?` · ${esc(k.from)}`:""}</div>
          ${k.body?`<div class="text-dim text-[0.8rem] my-1.5 whitespace-pre-wrap max-h-16 overflow-hidden">${esc(k.body).slice(0,220)}${k.body.length>220?"…":""}</div>`:""}
          <div class="flex gap-1.5 mt-1.5">
            <select class="font-mono text-[0.74rem] bg-bg border border-line rounded-md px-1.5 py-1 text-dim" onchange="setTk(${ti},'status',this.value)" title="status">${TST.map(v=>opt(v,st)).join("")}</select>
            <select class="font-mono text-[0.74rem] bg-bg border border-line rounded-md px-1.5 py-1 text-dim min-w-0 flex-1" onchange="setTk(${ti},'machine',this.value)" title="assign to a machine">${machineOpts(k)}</select>
          </div>
        </div>`).join("")|| `<div class="text-faint font-mono text-[0.8rem]">—</div>`}
    </div>`).join("")+`</div>`;
}
function renderPorts(){
  const rows=[]; S.machines.forEach(m=>(m.ports||[]).forEach(p=>rows.push({n:p.number||0,proto:p.proto||"tcp",serves:p.serves||"",mn:m.name||"",links:p.links||[]})));
  const key=r=>r.n+"/"+r.proto,count={}; rows.forEach(r=>count[key(r)]=(count[key(r)]||0)+1);
  rows.sort((a,b)=>a.n-b.n||a.proto.localeCompare(b.proto)||a.mn.localeCompare(b.mn));
  const vis=rows.filter(r=>!Q||(r.n+" "+r.proto+" "+r.serves+" "+r.mn+" "+r.links.map(l=>l.to+" "+l.detail).join(" ")).toLowerCase().includes(Q.toLowerCase()));
  if(!vis.length){ app.innerHTML=`<div class="text-dim text-center py-12 font-mono">no ports yet</div>`; return; }
  const th=`px-2.5 py-1.5 text-left font-mono font-medium text-[0.7rem] tracking-wider uppercase text-faint border-b border-line`;
  const td=`px-2.5 py-2 border-b border-line`;
  app.innerHTML=`<table class="w-full border-collapse text-[0.9rem]"><thead><tr><th class="${th}">port</th><th class="${th}">proto</th><th class="${th}">machine</th><th class="${th}">serves</th><th class="${th}">connects to</th></tr></thead><tbody>`+
    vis.map(r=>{const re=count[key(r)]>1;return `<tr>
      <td class="${td} font-mono ${re?"text-wip":"text-amber"}">${r.n||"?"}${re?' <span title="reused on multiple machines">⚠</span>':""}</td>
      <td class="${td} font-mono text-dim uppercase text-[0.82rem]">${esc(r.proto)}</td><td class="${td} font-mono">${esc(r.mn)}</td>
      <td class="${td}">${r.serves?esc(r.serves):`<span class="text-faint italic">${esc(WK[r.n]||"—")}</span>`}</td>
      <td class="${td} font-mono text-dim">${(r.links&&r.links.some(l=>l.to||l.detail))?r.links.filter(l=>l.to||l.detail).map(l=>`${l.to?`<span class="text-ink">${esc(l.to)}</span>`:""}${l.detail?` ${esc(l.detail)}`:""}`).join(`<span class="text-faint">, </span>`):`<span class="text-faint">—</span>`}</td></tr>`;}).join("")+`</tbody></table>`;
}
function mapW(){ return Math.max(app.clientWidth||960, 720); }
function ensurePositions(){
  let changed=false; const W=mapW(), cx=W/2, cy=300, n=S.machines.length, R=Math.min(cx-140, 90+n*24);
  S.machines.forEach((m,i)=>{ if(typeof m.x!=="number"||typeof m.y!=="number"){ if(n<=1){m.x=cx;m.y=cy;} else {const a=-Math.PI/2+2*Math.PI*i/n; m.x=Math.round(cx+R*Math.cos(a)); m.y=Math.round(cy+R*Math.sin(a));} changed=true; } });
  if(changed) save();
}
function autoArrange(){ S.machines.forEach(m=>{delete m.x;delete m.y;}); ensurePositions(); save(); render(); }
function svgPt(svg,ev){ const pt=svg.createSVGPoint(); pt.x=ev.clientX; pt.y=ev.clientY; return pt.matrixTransform(svg.getScreenCTM().inverse()); }
function updateMapEdges(name){ document.querySelectorAll(".medge").forEach(g=>{ const f=g.dataset.from,t=g.dataset.to; if(f!==name&&t!==name)return; const A=S.machines.find(m=>m.name===f),B=S.machines.find(m=>m.name===t); if(!A||!B)return; const [x1,y1]=edgePoint(A.x,A.y,B.x,B.y),[x2,y2]=edgePoint(B.x,B.y,A.x,A.y); const ln=g.querySelector("line"); ln.setAttribute("x1",x1);ln.setAttribute("y1",y1);ln.setAttribute("x2",x2);ln.setAttribute("y2",y2); const lg=g.querySelector(".medge-lblg"); if(lg) lg.setAttribute("transform",`translate(${(x1+x2)/2},${(y1+y2)/2})`); }); }
function wireMapNodes(){ const svg=document.getElementById("netmap"); if(!svg) return;
  svg.querySelectorAll(".mnode").forEach(g=>g.addEventListener("mousedown",ev=>{ ev.preventDefault(); const i=+g.dataset.i; const p=svgPt(svg,ev); mapDrag={svg,g,i,ox:p.x-S.machines[i].x,oy:p.y-S.machines[i].y,moved:false}; g.parentNode.appendChild(g); }));
}
function renderMap(){
  if(!S.machines.length){ app.innerHTML=`<div class="text-dim text-center py-12 font-mono">no machines yet — add one on the <b class="text-ink">machines</b> tab</div>`; return; }
  ensurePositions();
  const W=mapW(); let maxY=0; S.machines.forEach(m=>{maxY=Math.max(maxY,m.y||0);}); const H=Math.max(540,maxY+150);
  const byName={}; S.machines.forEach(m=>byName[m.name]=m);
  const edges=[]; S.machines.forEach(m=>(m.ports||[]).forEach(p=>(p.links||[]).forEach(l=>{ if(l.to&&l.to!=="external"&&byName[l.to]&&byName[l.to]!==m) edges.push({from:m,to:byName[l.to],label:(p.number||"?")+(l.detail?" "+l.detail:"")}); })));
  const edgeSVG=edges.map(e=>{ const [x1,y1]=edgePoint(e.from.x,e.from.y,e.to.x,e.to.y),[x2,y2]=edgePoint(e.to.x,e.to.y,e.from.x,e.from.y); const mx=(x1+x2)/2,my=(y1+y2)/2, w=e.label.length*6.6+12; return `<g class="medge" data-from="${esc(e.from.name)}" data-to="${esc(e.to.name)}"><line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" marker-end="url(#arrow)"/><g class="medge-lblg" transform="translate(${mx},${my})"><rect class="medge-lblbg" x="${-w/2}" y="-9" width="${w}" height="18" rx="4"/><text class="medge-lbl" text-anchor="middle" dy="0.32em">${esc(e.label)}</text></g></g>`; }).join("");
  const nodeSVG=S.machines.map((m,i)=>`<g class="mnode" data-name="${esc(m.name)}" data-i="${i}" transform="translate(${m.x},${m.y})"><title>${esc(m.name)}${m.host?" — "+esc(m.host):""} · click to edit</title>
      <rect class="mnode-box" x="${-HW}" y="${-HH}" width="${NW}" height="${NH}" rx="12"/>
      <circle class="mnode-led ${LED[m.status||'active']}" cx="${HW-13}" cy="${-HH+13}" r="4"/>
      <g class="mnode-icon" transform="translate(0,${-HH+24})"><rect x="-17" y="-12" width="34" height="24" rx="2.5" fill="none"/><line x1="0" y1="12" x2="0" y2="17"/><line x1="-9" y1="18" x2="9" y2="18"/></g>
      <text class="mnode-name" y="${HH-22}" text-anchor="middle">${esc(m.name||"?")}</text>
      <text class="mnode-sub" y="${HH-7}" text-anchor="middle">${(m.ports||[]).length}p · ${incomingFor(m.name).length}in</text></g>`).join("");
  app.innerHTML=`<div class="flex items-center gap-3 mb-2 flex-wrap">
      <span class="text-faint font-mono text-[0.78rem]">drag machines to arrange · arrows = port connections (set on the machines tab) · click a machine to edit it</span>
      <button class="btn ml-auto" onclick="autoArrange()">auto-arrange</button></div>
    <svg id="netmap" viewBox="0 0 ${W} ${H}" width="100%" height="${H}" style="touch-action:none">
      <defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z"/></marker></defs>
      <g id="edges">${edgeSVG}</g><g id="nodes">${nodeSVG}</g></svg>`;
  wireMapNodes();
}
document.querySelectorAll(".tab").forEach(b=>b.onclick=()=>{ document.querySelectorAll(".tab").forEach(x=>x.classList.remove("on")); b.classList.add("on"); TAB=b.dataset.tab; location.hash=b.dataset.tab; render(); });
window.addEventListener("mousemove",ev=>{ if(!mapDrag)return; const p=svgPt(mapDrag.svg,ev); const nx=Math.round(p.x-mapDrag.ox),ny=Math.round(p.y-mapDrag.oy); const m=S.machines[mapDrag.i]; if(Math.abs(nx-(m.x||0))>2||Math.abs(ny-(m.y||0))>2)mapDrag.moved=true; m.x=nx;m.y=ny; mapDrag.g.setAttribute("transform",`translate(${nx},${ny})`); updateMapEdges(m.name); });
window.addEventListener("mouseup",()=>{ if(!mapDrag)return; const d=mapDrag; mapDrag=null; d.g.style.cursor="grab"; if(d.moved){ save(); } else { TAB="machines"; location.hash="machines"; document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("on",x.dataset.tab==="machines")); render(); } });
document.getElementById("q").oninput=e=>{ Q=e.target.value; render(); };
document.getElementById("add").onclick=addM;
document.getElementById("addtk").onclick=newTicket;
load();
</script></body></html>"""

LOGIN_HTML = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>portbook — sign in</title>
<style>
  :root{color-scheme:dark} *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f0f11;color:#f3f4f6;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  .card{width:min(92vw,340px);background:#17171b;border:1px solid #2b2b32;border-radius:16px;padding:26px;box-shadow:0 20px 50px -24px rgba(0,0,0,.8)}
  .brand{display:flex;align-items:center;gap:8px;font:700 18px ui-monospace,Menlo,monospace}
  .dot{width:10px;height:10px;border-radius:50%;background:#f2b544;box-shadow:0 0 9px #f2b544}
  .sub{color:#6d7079;font:12px ui-monospace,monospace;margin:4px 0 18px}
  label{display:block;font:12px ui-monospace,monospace;color:#a6acb8;margin-bottom:6px}
  input{width:100%;background:#1e1e23;border:1px solid #2b2b32;border-radius:10px;padding:10px 12px;color:#f3f4f6;font-size:15px;outline:none}
  input:focus{border-color:#f2b544;box-shadow:0 0 0 3px rgba(242,181,68,.22)}
  button{width:100%;margin-top:14px;background:#f2b544;color:#17171b;border:0;border-radius:10px;padding:10px;font:600 14px ui-monospace,monospace;cursor:pointer}
  button:hover{filter:brightness(1.06)}
  .err{color:#ec6a6a;font:12px ui-monospace,monospace;min-height:16px;margin-top:10px}
</style></head><body>
  <form class="card" id="f" autocomplete="off">
    <div class="brand"><span class="dot"></span>portbook</div>
    <p class="sub">this instance is password-protected</p>
    <label for="pw">password</label>
    <input id="pw" type="password" autofocus>
    <button type="submit">sign in</button>
    <div class="err" id="err"></div>
  </form>
<script>
  const f=document.getElementById("f"),pw=document.getElementById("pw"),err=document.getElementById("err");
  f.onsubmit=async(e)=>{ e.preventDefault(); err.textContent="";
    let r; try{ r=await fetch("api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:pw.value})}); }
    catch(_){ err.textContent="can't reach portbook"; return; }
    if(r.ok){ location.href="/"; } else { err.textContent="wrong password"; pw.select(); }
  };
</script></body></html>"""

if __name__ == "__main__":
    main()
