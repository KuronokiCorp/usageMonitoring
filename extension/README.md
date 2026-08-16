# itermon control panel (Chrome extension)

An unpacked Manifest V3 Chrome extension for [itermon](../README.md). It does two things:

1. **Notifier core.** A background service worker polls itermon's local admin API on a
   timer (default once a minute) and raises a desktop notification when a new error shows
   up in the activity log, or when a scheduled job silently misses its slot. A badge on the
   toolbar icon shows the unread error count. This works with no tab open.
2. **Control panel.** A popup (also openable as a full tab) that lists your terminal
   sessions, lets you manage scheduled jobs (create, enable/disable, delete, run now), view
   the activity log, and send a one-off command to a chosen session — every destructive or
   executing action is behind a confirmation step.

It talks to the API from the background worker and the panel page only — there are no
content scripts, so it needs zero changes to itermon's server code.

## Load unpacked

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and pick this `extension/` folder.
4. Chrome assigns the extension an ID derived from the absolute path of this folder. If you
   move or rename the folder later, Chrome gives it a **new** ID.

## Finding the extension's ID

Open the extension's **Settings** tab — the "Server setup (one time)" section shows the
live `chrome-extension://<id>` origin and a ready-to-copy restart command. You can also see
the ID on `chrome://extensions` itself, under the extension's name.

## The one-time server-side step

By default itermon only accepts API requests from origins it already knows about (an
`Origin`/`Host` allowlist — see the main project's `README.md`). To let this extension
through, restart itermon once with:

```
python3 iterm_web.py --allow-origin chrome-extension://<the extension's id> --port <your port>
```

Scripts and `curl` are completely unaffected by this — they send no `Origin` header at all,
and requests with no `Origin` header are always allowed. This restart is the *only* server
change the extension needs; nothing in itermon's own code is modified.

## Port setting

Chrome extension `host_permissions` match patterns have no port component, so
`http://127.0.0.1/*` already covers whichever `--port` you run itermon on — you never need
to edit the manifest or reload the extension when you change ports. Just update the **Port**
field on the Settings tab to match. The default port shown there is itermon's own default
(see `iterm_web.py --help`).

## Permissions, and why each one is needed

- **`alarms`** — schedules the background poll on a fixed period via `chrome.alarms`
  (never `setInterval`), because an MV3 service worker is not persistent and a wall-clock
  timer would die with it.
- **`notifications`** — raises the desktop alert for a new error log line or a missed job
  slot.
- **`storage`** — persists settings (host/port/poll interval) and notifier dedup state
  locally on this machine (`chrome.storage.local`, never `storage.sync`).

Nothing else is requested: no `tabs`, no `activeTab`, no `scripting`, no `webRequest`, no
`<all_urls>`. There are no content scripts.

## Not part of the npm package, not on the Chrome Web Store

This `extension/` directory is not published to npm — `package.json`'s `files` array does
not include it, and the npm tarball is unaffected by its presence in this repository. It is
also not published on the Chrome Web Store; installing it means loading it unpacked, as
described above.
