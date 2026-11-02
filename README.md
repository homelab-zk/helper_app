# helper-app

Self-hosted chat helpers that answer from your own Markdown, PDF and Word files with the
[Claude Code](https://claude.com/claude-code) CLI, with an admin page for helpers, files and users.

Each **helper** is a small chat web app on its own port: it answers questions from its own documents,
cites where the answer comes from, and keeps every chat. An **admin page** creates and manages helpers.
Every question runs the Claude Code CLI (`claude -p`) read-only in the helper's document folder, so it uses
your Claude subscription; there is no separate API key.

## Features

- **Admin page** (port 8010): create helpers with a name, a purpose (Claude's instructions) and a port;
  start, stop, restart; process log. Tabs per helper: Settings, Files, Users, Log.
- **Files**: upload `.md`, `.markdown`, `.txt`, or `.pdf` and `.docx`, which are converted to Markdown on
  upload (the original is kept for download). Drag and drop.
- **Users per helper**: each helper has its own users (scrypt password hashes); everyone sees only their own
  chats. The admin account works everywhere. A new password or a removed user logs that person out at once.
- **Attachments**: up to 3 files per question (`.pdf`, `.docx`, `.txt`, `.md`, `.log`, `.csv`, `.json`, `.xml`,
  `.yaml`, `.ini`, `.conf`, `.cfg`; 10 MB each). They're converted to text on the server (PDF/DOCX locally, as
  for documents) and sent with the question, at most 100,000 characters per question; kept with the chat
  for download and removed with it.
- **Screenshots**: attach up to 4 per question (📎, paste with Ctrl+V, or drag and drop); they're scaled
  down in the browser, sent to Claude as images, and kept with the chat (visible only to its owner).
  Deleting a chat removes its screenshots and Claude Code's own transcript of it.
- **Chat page**: streaming answers, the lookups Claude made, model (Opus / Sonnet / Haiku) and effort per
  question, an optional per-question **Web search** switch, chat search, installable as an app (PWA) over HTTPS.
- **Supervision**: helpers are started automatically and restarted after a crash.
- **Small**: Python standard library only for the app; two vendored browser libraries (marked, DOMPurify).
  Nothing of Claude runs while nobody is asking a question.

## Requirements

- Linux with **Python 3.11+** and systemd (for running it as a service).
- **Claude Code** installed and logged in for the user that runs the app (`claude`, then `/login`).
  Default path `~/.local/bin/claude`.
- For PDF uploads: a Python virtual environment with **pymupdf4llm 1.28.2** (version 2.0 on PyPI is an
  empty package). Without it, `pdftotext` (poppler-utils) is used and gives plain text. DOCX needs nothing.
- For phones and "install as app": an HTTPS reverse proxy in front of each helper (Nginx, Caddy,
  Cloudflare Tunnel, ...).

## Security and privacy

- **What leaves your machine**: each question, its screenshots, the text of attached files and the document excerpts Claude reads are sent to Anthropic,
  as with any Claude Code use. With Web search ticked, Claude also sends short search queries to a search
  service (it is told never to put document text in them). PDF/DOCX conversion is local.
- **What Claude can do**: only `Read`, `Grep` and `Glob` in the helper's own `docs/` folder
  (`--restricted`, no MCP servers, no slash commands), plus `WebSearch` when ticked. No shell, no file
  editing, no web fetch.
- **Logins**: random session token in an `HttpOnly`, `SameSite=Strict` cookie (30 days, `Secure` behind an
  HTTPS proxy that sends `X-Forwarded-Proto: https`); only a SHA-256 hash of it is stored. Wrong logins wait
  1 second. **Without a credential file and without helper users, login is off**: don't expose the app then.
- **Files**: everything under `data/` is created readable by the owner only.
- **Keep the admin page on your LAN** (or behind a VPN). Put only helpers behind a public proxy, and consider
  an extra access layer (for example Cloudflare Access) for those.
- `data/` (documents, chats, users) never belongs in git; `.gitignore` excludes it.

## Setup

Run these as the user that should run the app (not root, and without `sudo` unless shown).

1. **Get the code**

   ```bash
   git clone <repo-url> ~/helper-app
   cd ~/helper-app
   ```

2. **Claude Code**: install it and log in once (`claude`, then `/login`). Check: `~/.local/bin/claude --version`.

3. **PDF conversion (optional)**

   ```bash
   python3 -m venv ~/.venvs/pdf2md
   ~/.venvs/pdf2md/bin/pip install pymupdf4llm==1.28.2
   ```

4. **Admin login**: one line `user:password` (pick your own):

   ```bash
   mkdir -p ~/.config/conv-helper && chmod 700 ~/.config/conv-helper
   printf '%s\n' 'admin:<password>' > ~/.config/conv-helper/credential
   chmod 600 ~/.config/conv-helper/credential
   ```

5. **Try it**

   ```bash
   python3 manager.py
   # helper admin on http://0.0.0.0:8010  data=.../data  login=on
   ```

   Open `http://<server-ip>:8010`, log in, click **+ New helper**. Ctrl+C stops it and all helpers.

6. **Run it as a service**

   ```bash
   mkdir -p ~/.config/systemd/user                       # not there yet on a fresh system
   cp helper-admin.service.example ~/.config/systemd/user/helper-admin.service
   nano ~/.config/systemd/user/helper-admin.service     # only if not cloned to ~/helper-app
   systemctl --user daemon-reload
   systemctl --user enable --now helper-admin
   sudo loginctl enable-linger "$USER"                   # keep running without a login
   systemctl --user status helper-admin                  # log; journalctl --user may have none
   ```

7. **HTTPS for a helper (optional)**: point a proxy host at `http://<server-ip>:<helper-port>`, pass
   `X-Forwarded-Proto`, and set a read timeout of 600 s (answers can take a while). Streaming already works
   through nginx (`X-Accel-Buffering: no`).

### Settings

All optional, as environment variables (for example `Environment=` lines in the service file):

| Variable | Default | Meaning |
|---|---|---|
| `CONV_HELPER_ADMIN_PORT` | `8010` | Admin page port |
| `CONV_HELPER_HOST` | `0.0.0.0` | Listen address (admin page and helpers) |
| `CONV_HELPER_DATA` | `./data` | Data folder |
| `CONV_HELPER_CREDENTIAL` | `~/.config/conv-helper/credential` | Admin login file (`user:password`) |
| `CONV_HELPER_CLAUDE` | `~/.local/bin/claude` | Claude Code binary |
| `CONV_HELPER_MODEL` | `opus` | Default model when the browser doesn't choose |
| `CONV_HELPER_PDF_PYTHON` | `~/.venvs/pdf2md/bin/python` | Python that has pymupdf4llm |

## Update

```bash
cd ~/helper-app
git pull
systemctl --user restart helper-admin     # needed for .py changes; restarts every helper
```

Page, style and script changes are picked up on the next page load without a restart. `data/` and your
credential file are not touched by `git pull`.

## Moving to another machine

Stop the service, then copy `data/` and `~/.config/conv-helper/credential` to the new machine, clone the
code there, and follow the setup above (Claude Code must be logged in there too). Chats continue; follow-up
questions in old chats start from the saved messages, because Claude's own session files stay behind.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Admin page or helper says `login=OFF` in the log | No credential file (and no helper users). Create it (setup step 4) and restart. |
| "Port N is in use by another program" | Something else listens there. Pick another port, or stop that program. |
| Helper shows **Failed** | It crashed 5 times in a row. Read the **Log** tab, fix the cause, press **Start**. |
| Answer ends with `**Error:** ...` | Usually Claude Code isn't logged in for this user, or the usage limit is reached. Run `claude` as that user. |
| PDF upload: "very little text found" | A scanned PDF (images only). There is no OCR: convert it to Markdown yourself and upload that. |
| `.doc` upload refused | Old Word format: save it as `.docx` first. |
| Answers arrive all at once behind a proxy | The proxy buffers responses; make sure it honours `X-Accel-Buffering: no` or disable buffering. |
| "Earlier Claude session not found" | Claude Code cleaned up the session, or the data was moved. The chat continues from the saved messages. |

## How it works

```
manager.py   admin page (port 8010) + supervisor: one helper.py process per enabled helper
helper.py    one helper: chat page and API on its own port; runs claude -p for each question
common.py    login sessions, password hashes, responses
convert.py   PDF (pymupdf4llm, fallback pdftotext) and DOCX (standard library) to Markdown
static/      admin page, chat page, login page, icons, vendored marked and DOMPurify
data/helpers/<id>/
  helper.json  name, purpose, port, enabled
  docs/        the Markdown files Claude may read (its working directory)
  originals/   uploaded PDF/DOCX files
  chats.db     SQLite: chats (with owner), messages, sessions, users
```

For each question the helper starts:

```
claude -p --output-format stream-json --verbose --include-partial-messages \
  --restricted --strict-mcp-config --disable-slash-commands --tools Read,Grep,Glob \
  --append-system-prompt "<template + name + purpose + file list>" \
  --model <opus|sonnet|haiku> [--effort <level>] [--resume <session-id>]
```

reads the streamed events, forwards the text and lookups to the browser, and saves the answer. The process
exits after the answer; follow-ups resume the chat's Claude session.

## License

MIT, see [LICENSE](LICENSE). `static/vendor/` contains marked (MIT) and DOMPurify (Apache 2.0 / MPL 2.0),
each with its own license header.
