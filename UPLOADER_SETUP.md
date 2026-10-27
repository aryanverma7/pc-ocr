# Send to editor: setup

Right-click one or more videos in Explorer, choose **Send to editor**, and they
go straight to the editor's Google Drive. Nothing passes through the Mac Mini.
A small icon in the system tray shows progress.

## One-time setup

1. **Update the Mac Mini first.** Its backend update (the uploader routes)
   must be merged and pulled on the Mac Mini, and the backend restarted, before
   anything here will work. Then, on this PC, open a terminal in the agent
   folder (the one with `agent.py`) and run:

   ```
   git pull
   ```

2. **Install what it needs:**

   ```
   pip install -r requirements.txt
   ```

3. **Let the uploader ask OBS whether you are live**, so it never slows your
   stream down. In OBS: **Tools -> WebSocket Server Settings**.
   - Tick **Enable WebSocket server**.
   - Leave the port at **4455**.
   - Tick **Enable Authentication**.
   - Click **Show Connect Info** and copy the **Server Password**.
   - Click **OK**.

4. **Give the uploader that password.** Open `agent_config.json` in the agent
   folder. Put a new line **right after the opening `{`** and paste this into
   it, with your password and **with the comma at the end**:

   ```
   "obs_ws_password": "<the password>",
   ```

   Putting it first, with its comma, keeps the file valid whatever comes after.
   The result looks like this (your other lines stay as they were):

   ```
   {
     "obs_ws_password": "abc123XYZ",
     "backend_url": "https://hub.dualbladex.org/api/ocr/credit-report",
     "agent_secret": "...",
     ...
   }
   ```

   If you changed the OBS port from 4455, add `"obs_ws_port": <your port>,` on
   the next line the same way; otherwise leave it out. Nothing else is needed: the uploader reuses `backend_url` and
   `agent_secret`, which are already there for the credit reader.

5. **Add the right-click menu entry:**

   ```
   python install_uploader.py
   ```

6. **Try it with one short clip.** Right-click a clip under 5 minutes and
   choose **Send to editor** (on Windows 11 it is under **Show more options**).
   The tray icon should appear, tell you the clip's length and that it is a
   clip, and disappear when it has been sent.

## Using it

- Select up to **15 files** per right-click (Windows' own limit; do another
  right-click for more). Each file becomes its own project for the editor.
- Anything **under 5 minutes is a clip**, anything longer is a VOD.
- Hover over the tray icon to see what is sending and how far along it is.
- **While OBS is streaming, uploads pause** so your stream keeps all the
  bandwidth. They carry on 5 minutes after you stop streaming.
- To stop early, right-click the tray icon and choose **Stop (resumes at next
  login)**. Nothing is lost.
- After a **power cut or restart**, unfinished uploads carry on by themselves
  at your next login, from where they stopped.
- The log is `uploads\uploader.log` in the agent folder.
- To remove the menu entry and the resume-at-login: `python install_uploader.py --uninstall`

## If something goes wrong

- **The tray says "The agent secret is wrong"**: `agent_secret` in
  `agent_config.json` no longer matches the Mac Mini's `ocr_agent_secret`. Fix
  it and send the files again (or log out and back in; the queue is kept).
- **"Drive is too full"**: free up space on the editor's Google Drive, then
  send the file again.
- **Nothing happens on right-click / no tray icon / no "Send to editor"
  entry**: first check `agent_config.json` for a missing or extra comma - a
  broken file stops both the uploader and the credit reader. Then look at the
  end of `uploads\uploader.log`. If neither explains it, run
  `python install_uploader.py` again (also needed if you move the agent folder
  or reinstall Python).
- **The tray says "The Mac Mini backend needs updating before the uploader can
  work"**: the Mac Mini is still on the old backend. Pull the update there and
  restart the backend (step 1); your files stay queued and go at the next
  right-click or login.
- **Uploads don't pause while you are live**: the OBS password is probably
  wrong (redo steps 3 and 4). Until it is fixed the uploader asks the Mac Mini
  instead, which only knows you are live while go-live announcements are
  switched on; otherwise it just uploads.
- **Anything else**: look at the end of `uploads\uploader.log`.
