# Polaron V1.31.2: "This computer" folder chooser, deployed and completed

## Why the old dialog was still shown
The "Select evidence directory on this computer" dialog in use still had the old text ("Open a drive on this
computer…", "Nothing is uploaded…") and no **Choose folder on this computer…** button. The V1.31.0 website was
never built on the server:
- `start_docker_prod.cmd` builds the console website ("Build HTTPS gateway website") only **after** it has added
  Polaron to Caddy;
- the last start stopped at that Caddy step because Caddy was not running (fixed in V1.31.1);
- the containers that were already running kept serving the previous website.

## Seeing the client computer's drives and folders
A web page cannot list a computer's drives by itself. "This computer" offers two ways:

1. **No install (V1.31.0):** **Choose folder on this computer…** opens Windows' own folder window on the client.
   It shows This PC with C:, D:, USB disks and mapped drives. Browse to the image folder and select it.
2. **Client Evidence Agent (one-time install):** the dialog then lists the client's drives and folders inside the
   page, like the Forensic server tab. The yellow panel in your screenshot means this client has no agent, or the
   browser blocks it.

## New in V1.31.2
- **Choose image files…** sits next to the folder button. Browsers do not accept a whole drive (for example `E:\`)
  as a folder. For an image stored directly on a drive, select every segment instead (`laptop.E01`, `laptop.E02`,
  …). For mobile, select the `.zip` or `.pas` file. The files are served through the tab exactly like a folder;
  the case is named after the image (`laptop`).
- **Mobile acquisition, "Copy this folder to the forensic server":** a case folder chosen in the browser now
  works without the agent. Before, it failed with "The Client Evidence Agent is not running".
  - The tab serves the folder over the outbound connection. The server copies the newest sealed run, checks
    every file with SHA-256, and the tab stops serving when the copy ends.
  - That screen offers the folder window only, because it needs the whole case folder.
  - Server side: `POST /api/acquisition/client-import` accepts `browser: true` and then reaches the PC only
    through the outbound connection (no LAN addresses).

## What you do
1. Unzip `POLARON_V1_31_2_UPDATED_FILES.zip` over the project root. It holds V1.31.0, V1.31.1 and V1.31.2
   together. No `.env` change is needed.
2. Run `script_docker\start_docker_prod.cmd` **as Administrator**:
   - it starts Caddy if Caddy is stopped (V1.31.1);
   - it rebuilds the website with the new dialog ("Build HTTPS gateway website") and the backend;
   - it restarts the containers.
3. In the browser on the client, reload the console with **Ctrl+F5**.
4. Check the result. **This computer** must show a blue box "Choose a folder on this computer" with the buttons
   **Choose folder on this computer…** and **Choose image files…**. The agent panel is folded underneath as
   "Optional: …".

## Tests
- Frontend:
  - `tsc` clean, vitest 63/63, `vite build` OK.
  - New tests:
    - image files chosen one by one (flat list, name, digest);
    - dialog flow with **Choose image files…**;
    - the option hidden where a whole case folder is required;
    - mobile copy of a browser-chosen folder with no agent call (sends `browser: true`, stops serving afterwards);
    - mobile copy that the server refuses also stops serving.
- Backend: 66 passed, 1 skipped. New tests:
  - mobile copy of a browser-chosen folder: relay only, no LAN lookup, copy completes;
  - refused when the outbound connection is turned off;
  - the endpoint passes the `browser` flag.
- Launcher (V1.31.1): 90 offline Caddy checks.

Originals: `recovery-snapshots/pre-v1.31.0-browser-folder-20261116/`,
`recovery-snapshots/pre-v1.31.1-caddy-autostart-20261116/`, `recovery-snapshots/pre-v1.31.2-client-folder-20261116/`.
