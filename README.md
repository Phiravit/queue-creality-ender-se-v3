# 3D Print Queue

A plain web page in front of the two OctoPrint printers. Students upload G-code and
see the queue, progress and printer errors. The queue starts the next job by itself,
**but only after someone confirms the plate is empty**. Discord alerts are optional.

## Why "Plate is clear" is a button, not a sensor

OctoPrint and the Ender 3 have no sensor that knows whether something is on the bed.
So the queue **fails locked**: after every finished, cancelled or failed print (and
after a restart or a printer error), the printer goes to **Waiting for the plate to
be cleared**. Nothing prints until a person at the printer ticks "the plate is
completely empty" and presses the button. Their name is logged.

If the printers get a camera, set `PRINTERx_SNAPSHOT` and the photo appears next to
the button, so staff can check from anywhere.

## What students see

| State | Meaning |
|---|---|
| Ready (green) | Plate confirmed empty. The next job starts within seconds. |
| Printing (blue) | Progress, time left, temperatures. The owner can pause or cancel. |
| Waiting for the plate to be cleared (orange) | A print ended. Remove it, then press **Plate is clear**. |
| Printer error / Offline (red) | What OctoPrint reports (e.g. thermal runaway). Students can't unlock it; staff fix it. |

Students can only remove or cancel their **own** jobs (remembered by their browser).
A print started directly in OctoPrint (not through the queue) is still detected: the
printer shows as busy and locks afterwards.

The queue can only report what OctoPrint knows. It **can't** see:
- spaghetti, a print that came loose, or an empty spool;
- a print started from the printer's own screen and SD card (OctoPrint never hears about it);
- whether the person pressing **Plate is clear** is really at the printer. It's an honour
  system with their name logged. A camera snapshot (`PRINTERx_SNAPSHOT`) lets staff check.

If the queue loses contact with OctoPrint mid-print, it keeps the job and shows
"Can't reach this printer". It doesn't mark the print failed.

## Staff page (`/admin`)

Log in with `ADMIN_PASSWORD`. Staff can reorder or remove any job, pause or resume a
printer's queue, lock a printer, cancel any queue print, mark a plate clear even when
the printer reports a problem, and read the event log.

## Install on the Raspberry Pi

Run these on the Pi (SSH in as you normally do). Replace `<pi-user>` and `<pi-ip>` with your Pi login and address; the service file assumes the user `pi`, so change
them if your user is different.

1. Copy this folder to the Pi, e.g. from your laptop:
   `scp -r print-queue <pi-user>@<pi-ip>:~/` (only `app.py`, `templates/`,
   `requirements.txt`, `config.example.env` and `print-queue.service` are needed).
2. Install:
   ```bash
   cd ~/print-queue
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   cp config.example.env config.env
   nano config.env
   ```
3. Fill in `config.env`:
   - `ADMIN_PASSWORD`: a new staff password (not the OctoPrint or Pi one).
   - `PRINTER1_APIKEY` and `PRINTER2_APIKEY`: in each OctoPrint (5001 and 5002), log in as
     its admin, then **Settings > Application Keys > Generate**. (Or **Settings > API >
     Global API Key**.) Each printer has its own key.
   - `PRINTERx_URL` stays `http://127.0.0.1:5001` / `5002` when the queue runs on the same Pi.
   - Discord (optional): see below.
4. Try it: `venv/bin/python app.py`, then open `http://<pi-ip>:8000` from a lab
   computer. Press Ctrl+C to stop.
5. Make it start on boot:
   ```bash
   sudo cp print-queue.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now print-queue
   journalctl -u print-queue -f     # live log
   ```

After every restart the printers start in **Waiting for the plate to be cleared**.
That's deliberate.

## First run on the real printers

Everything so far was tested against a fake OctoPrint. Before letting students use it:

1. With the queue running, open the page while **nothing** is queued. Both printers should
   show their real state and temperatures (this only reads from OctoPrint).
2. Start a short print **in OctoPrint directly**. The queue should show it as "Not from the
   queue" with a progress bar. **Check the percentage looks right (e.g. 37%, not 0.37%).**
   When it ends, the printer should lock with "Print finished" (if it says "Stopped at 1%",
   OctoPrint reports progress as a fraction; tell whoever maintains `app.py`).
3. Then queue a small test G-code through the page and watch it start, finish and lock.

## Discord alerts

1. In Discord: the channel's **Edit Channel > Integrations > Webhooks > New Webhook >
   Copy Webhook URL**.
2. Paste it as `DISCORD_WEBHOOK_URL` in `config.env` and restart:
   `sudo systemctl restart print-queue`.
3. Optional: `DISCORD_STAFF_ROLE_ID` pings a staff role on errors only. (In Discord,
   turn on Developer Mode, then right-click the role and choose **Copy Role ID**.)

Alerts are sent when a print starts, finishes (with a reminder to clear the plate),
is stopped, or fails, when a printer locks because of an error, and when someone
prints outside the queue. Keep the webhook URL private: anyone who has it can post
to the channel.

## Make the queue the only way in

Students who still have the shared OctoPrint logins can skip the
queue. The queue notices those prints and locks afterwards, but for a fair queue,
change those passwords so only staff use OctoPrint directly.

## Testing without the real printers

`dev/fake_octoprint.py` pretends to be OctoPrint, and `dev/e2e_test.py` runs the
whole flow against two fakes (13 checks, no real printer touched):

```bash
pip install -r requirements.txt
python dev/e2e_test.py
```

## Files

| File | What it is |
|---|---|
| `app.py` | Web app, OctoPrint poller, queue rules, Discord alerts |
| `templates/` | The pages (student page, staff page, login) |
| `config.example.env` | Settings template; copy to `config.env` |
| `print-queue.service` | systemd unit to start on boot |
| `data/` | Created at runtime: SQLite database and uploaded files |
| `dev/` | Fake OctoPrint and the end-to-end test |
