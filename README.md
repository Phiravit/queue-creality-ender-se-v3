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

## Sign-in

The first time, a student signs in with their student ID and full name. After that
the device remembers them for about four months, and on a new device the student ID
alone is enough (the queue remembers the name). Jobs belong to the student ID, so a
student can cancel their print from any device they're signed in on.

There is no password: it's an honour system for a small closed lab, and it records
who printed what. Anyone who types someone else's ID could act as them. The ID must
be 6–15 digits; change `STUDENT_ID_PATTERN` in `config.env` (a regular expression)
to accept only SIT IDs if you know their format.

### Printer password

To keep a printer (e.g. the Research printer) for people you've told, set
`PRINTER2_PASSWORD=...` in `config.env`. Students then need that password to queue
on it. Each student types it once: after that it's remembered for their student ID on
every device. Changing the password asks everyone again. Staff never need it. Plate
clearing stays open to everyone at the printer.

### Printer tabs

The student page shows one printer at a time, with a tab per printer at the top
(each tab shows a live status, e.g. "Printing 42% · 2 waiting"). Each printer has
its own "Add a print" form, so there's no printer to pick from a list. The page
remembers the last tab a student used. The staff page still shows every printer.

### Night mode

The page follows the device's dark/light setting. The moon/sun button in the header
switches it, and the choice is remembered on that device.

## Jobs

Students can only remove or cancel their **own** jobs.
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

1. Copy the app to the Pi from your laptop (you type the Pi password):
   `scp -r print-queue <pi-user>@<pi-ip>:~/`
   Re-running this later updates the code but never touches `config.env` or `data/` on the Pi.
2. Run the installer on the Pi (it asks for the sudo password when needed):
   `ssh -t <pi-user>@<pi-ip> "bash ~/print-queue/install.sh"`
   It creates a Python venv, installs the packages, creates `config.env` and opens it in
   nano the first time, installs the `print-queue` systemd service for your user, starts
   it and prints the last log lines.
3. In `config.env` fill in:
   - `ADMIN_PASSWORD`: a new staff password (not the OctoPrint or Pi one).
   - `PRINTER1_APIKEY` and `PRINTER2_APIKEY`: in each OctoPrint (5001 and 5002), log in as
     its admin, then **Settings > Application Keys > Generate**. (Or **Settings > API >
     Global API Key**.) Each printer has its own key.
   - `PRINTERx_URL` stays `http://127.0.0.1:5001` / `5002` when the queue runs on the same Pi.
   - `PUBLIC_URL` and Discord (optional): see below.
4. After editing `config.env` later: `sudo systemctl restart print-queue`.
   Live log: `journalctl -u print-queue -f`.

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
2. Paste it as `DISCORD1_WEBHOOK_URL` in `config.env` and restart:
   `sudo systemctl restart print-queue`.
3. Optional: `DISCORD1_STAFF_ROLE_ID` pings a staff role on errors only. (In Discord,
   turn on Developer Mode, then right-click the role and choose **Copy Role ID**.)

More servers: add `DISCORD2_WEBHOOK_URL` / `DISCORD2_STAFF_ROLE_ID`, `DISCORD3_...` and
so on (up to 9). Each webhook gets its own status board; a staff role only works in
the server it belongs to, so each webhook has its own. `DISCORDn_PRINTERS=2` makes a
webhook follow only printer 2 (alerts and board); leave it empty for every printer.

Alerts are sent when someone joins a queue, when a print starts, finishes (with a
reminder to clear the plate and who's next), is stopped, or fails, when a printer
locks because of an error, and when someone prints outside the queue. Keep the
webhook URL private: anyone who has it can post to the channel.

**Status board.** The queue also posts one "3D printer status" message and keeps
editing it: each printer's state, progress, time left and full queue, with a link to
that printer's tab. It updates at most every 20 s (`DISCORD_BOARD_SECONDS`) and at
least every 5 minutes, so the "Last updated" time shows whether the queue is alive.
Pin that message in the channel. If someone deletes it, a new one is posted. Turn it
off with `DISCORD_STATUS_BOARD=0`.

## Make the queue the only way in

Students who still have the shared OctoPrint logins can skip the
queue. The queue notices those prints and locks afterwards, but for a fair queue,
change those passwords so only staff use OctoPrint directly.

## Testing without the real printers

`dev/fake_octoprint.py` pretends to be OctoPrint, and `dev/e2e_test.py` runs the
whole flow against two fakes (14 checks, no real printer touched):

```bash
pip install -r requirements.txt
python dev/e2e_test.py
```

## Files

| File | What it is |
|---|---|
| `app.py` | Web app, OctoPrint poller, queue rules, Discord alerts |
| `templates/` | The pages (student page, sign-in, staff page, staff login) |
| `config.example.env` | Settings template; copy to `config.env` |
| `print-queue.service` | systemd unit to start on boot |
| `install.sh` | Installer/updater to run on the Pi |
| `data/` | Created at runtime: SQLite database and uploaded files |
| `dev/` | Fake OctoPrint and the end-to-end test |
