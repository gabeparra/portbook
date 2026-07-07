# portbook

A dead-simple **local** tracker for your machines — what each one does, the **ports** it uses,
and the **tickets** open against it. One Python file, no server to deploy, no Docker, no login.

## Run it
```bash
python3 portbook.py
```
That's the whole install — no `pip`, no venv, no Docker, no root. It:
- serves a small visual UI on **`http://127.0.0.1:8099`** — **loopback only**, never reachable
  from any network,
- auto-opens your browser,
- saves everything to **`portbook.json`** next to the script.

`Ctrl+C` to stop. Nothing runs in the background; nothing is exposed.

Options: `--port 9000`, `--file /path/to/other.json`, `--no-open`.
Handy alias: `alias portbook='python3 ~/path/to/portbook.py'`.

## What you track
- **Machines** — name, **hostname / FQDN / IP**, what it does (role), status, and optional
  OS/location/notes
- **Ports** — per machine: number + tcp/udp + what it serves + **what it connects to** — pick
  one of your existing machines from a dropdown (or "external"), with an optional port/detail,
  so connections link to real machines. Common
  ports auto-suggest a label (22 → SSH/SFTP, 80 → HTTP, 443 → HTTPS, 3306 → MySQL, …) — shown
  as a hint and filled in if you leave it blank. The **Port map** view lists every port with
  its machine, service, and connection in one table.
- **Tickets** — mostly **references to external tickets** (e.g. from the org that manages your
  ports / internet). Per ticket: status, priority, their **ref #**, a **link** to the external
  ticket (the ref becomes a clickable **↗** that opens it when a link is set), the **org** it's
  from, a title, and optional notes/body. Track them on a status board. Create a ticket
  **on its own** (header **+ ticket**) and **assign it to a machine** whenever — or leave it
  unassigned (it shows in an "unassigned" card and on the board).

Three views (toggle at the top):
- **Machines** — cards you edit inline
- **Tickets** — a board grouped by status, across all machines
- **Port map** — every port sorted by number, flagging any port reused on multiple machines

Plus instant search across everything, a **Print** view (clean one-page fleet sheet), and a
**Backup** button (downloads a dated JSON).

## Your data
`portbook.json` is the single source of truth — a **local, gitignored** file on your machine,
plain, human-readable, hand-editable. Everything saves **as you type** (nothing to lose).
- **Back up:** `cp portbook.json portbook.$(date +%F).json`, or `git commit` it, or drop it in a synced folder.
- **Restore / move:** copy the file back and restart (or `--file` it).
- Every save is atomic and keeps the prior copy as `portbook.json.bak` (one-step undo).
- Edit it by hand only while the server is stopped (the running UI owns the file).

## Restyle it (optional)
The UI is styled with **Tailwind, compiled to plain CSS and inlined** into `portbook.py` — so
it ships as one self-contained file with **no runtime dependencies**. To change the look:
```bash
# edit the tokens/components in build/input.css, then:
bash build/rebuild.sh        # recompiles + re-inlines the CSS (Node-free; needs internet once
                             # to fetch the Tailwind standalone binary)
```
That regenerates the `<style>` block in `portbook.py`. Running the app never needs the build.

## What it deliberately is NOT
No server/Docker/DB/nginx/auth/SSO, no network scanning, no background service, no sync.
Python standard library + a browser at runtime (Tailwind is build-time only, precompiled and
inlined). At ~5 machines, that's the right amount of tool.
