"""Print queue for OctoPrint printers.

Students upload G-code to a queue. The queue sends the next job to OctoPrint
only when the printer is idle AND someone has confirmed the plate is empty.

Each printer has a lock:
    READY        plate confirmed empty; the next queued job starts by itself
    BUSY         printing (started by the queue or by anyone else)
    NEEDS_CLEAR  a print ended, stopped or errored (or the app restarted):
                 nothing starts until a person presses "Plate is clear"

OctoPrint has no sensor for "something is on the bed", so the queue fails
locked: whenever it is unsure, it waits for a human.
"""
import os
import re
import sqlite3
import secrets
import threading
import time
import uuid
import logging
from contextlib import contextmanager
from datetime import datetime

import requests
from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.utils import secure_filename

BASE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("queue")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------------------------------------------------------------- config

def load_env(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env(os.environ.get("QUEUE_CONFIG", os.path.join(BASE, "config.env")))

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "queue.db")
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "5"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
START_GRACE = float(os.environ.get("START_GRACE_SECONDS", "30"))
BAD_POLLS_BEFORE_LOCK = int(os.environ.get("BAD_POLLS_BEFORE_LOCK", "3"))
ALLOWED_EXT = (".gcode", ".gco", ".g")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_STAFF_ROLE = os.environ.get("DISCORD_STAFF_ROLE_ID", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

os.makedirs(UPLOAD_DIR, exist_ok=True)


def notify(text, staff=False):
    """Post to the Discord channel webhook in the background. Never raises."""
    if not DISCORD_WEBHOOK:
        return
    if PUBLIC_URL:
        text += "\n" + PUBLIC_URL
    payload = {"content": text[:1900], "allowed_mentions": {"parse": []}}
    if staff and DISCORD_STAFF_ROLE:
        payload["content"] = "<@&%s> %s" % (DISCORD_STAFF_ROLE, payload["content"])
        payload["allowed_mentions"]["roles"] = [DISCORD_STAFF_ROLE]

    def send():
        try:
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        except requests.RequestException:
            log.warning("Discord webhook failed")
    threading.Thread(target=send, daemon=True).start()


class Printer:
    def __init__(self, pid, name, url, key, snapshot):
        self.id = pid
        self.name = name
        self.url = url.rstrip("/")
        self.key = key
        self.snapshot = snapshot
        self.bad_polls = 0

    def _h(self):
        # X-Api-Key for OctoPrint 1.x, Bearer for 2.x. Sending both is harmless.
        return {"X-Api-Key": self.key, "Authorization": "Bearer " + self.key}

    def status(self):
        """Read OctoPrint and boil it down to one dict the pages can show."""
        st = {"kind": "unreachable", "text": "Cannot reach OctoPrint", "error": "",
              "file": None, "completion": None, "time_left": None, "print_time": None,
              "tool": None, "bed": None, "ready": False, "paused": False}
        try:
            rj = requests.get(self.url + "/api/job", headers=self._h(), timeout=5)
            rp = requests.get(self.url + "/api/printer", headers=self._h(), timeout=5)
        except requests.RequestException as e:
            st["error"] = str(e.__class__.__name__)
            return st
        if rj.status_code in (401, 403):
            st["text"] = "OctoPrint rejected the API key"
            return st
        if rj.status_code != 200:
            st["text"] = "OctoPrint answered HTTP %s" % rj.status_code
            return st
        job = rj.json()
        prog = job.get("progress") or {}
        st["text"] = job.get("state") or "Unknown"
        st["error"] = job.get("error") or ""
        st["file"] = ((job.get("job") or {}).get("file") or {}).get("display") or \
                     ((job.get("job") or {}).get("file") or {}).get("name")
        st["completion"] = prog.get("completion")
        st["time_left"] = prog.get("printTimeLeft")
        st["print_time"] = prog.get("printTime")

        if rp.status_code == 409:          # printer not connected to OctoPrint
            st["kind"] = "error" if "error" in st["text"].lower() else "offline"
            return st
        if rp.status_code != 200:
            st["kind"] = "offline"
            return st
        pr = rp.json()
        flags = (pr.get("state") or {}).get("flags") or {}
        temp = pr.get("temperature") or {}
        st["tool"] = temp.get("tool0")
        st["bed"] = temp.get("bed")
        st["paused"] = bool(flags.get("paused") or flags.get("pausing"))
        st["ready"] = bool(flags.get("ready"))
        busy = any(flags.get(f) for f in
                   ("printing", "paused", "pausing", "cancelling", "resuming", "finishing"))
        if flags.get("error") or st["error"] or "error" in st["text"].lower():
            st["kind"] = "error"
        elif busy:
            st["kind"] = "busy"
        elif flags.get("closedOrError") or not flags.get("operational"):
            st["kind"] = "offline"
        else:
            st["kind"] = "idle"
        return st

    def upload_and_print(self, path, octo_name):
        with open(path, "rb") as f:
            r = requests.post(self.url + "/api/files/local", headers=self._h(),
                              files={"file": (octo_name, f, "application/octet-stream")},
                              data={"select": "true", "print": "true"}, timeout=300)
        r.raise_for_status()
        body = r.json() if r.content else {}
        if body.get("effectivePrint") is False:
            raise RuntimeError("OctoPrint stored the file but did not start the print")

    def job_command(self, payload):
        r = requests.post(self.url + "/api/job", headers=self._h(), json=payload, timeout=10)
        r.raise_for_status()

    def delete_file(self, octo_name):
        try:
            requests.delete(self.url + "/api/files/local/" + octo_name,
                            headers=self._h(), timeout=10)
        except requests.RequestException:
            pass


PRINTERS = {}
for i in range(1, 10):
    url = os.environ.get("PRINTER%d_URL" % i)
    if url:
        pid = str(i)
        PRINTERS[pid] = Printer(pid, os.environ.get("PRINTER%d_NAME" % i, "Printer %d" % i), url,
                                os.environ.get("PRINTER%d_APIKEY" % i, ""),
                                os.environ.get("PRINTER%d_SNAPSHOT" % i, ""))

# Latest OctoPrint reading per printer, filled by the poller thread.
LIVE = {pid:{"kind": "unreachable", "text": "Starting...", "error": ""} for pid in PRINTERS}


# ---------------------------------------------------------------- database

@contextmanager
def tx():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with tx() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer TEXT NOT NULL,
            student_name TEXT, student_id TEXT, note TEXT,
            filename TEXT, stored TEXT, octo_name TEXT, est_seconds INTEGER,
            status TEXT NOT NULL DEFAULT 'queued',   -- queued printing done stopped failed removed
            result TEXT, sort REAL NOT NULL,
            created REAL, started REAL, finished REAL);
        CREATE TABLE IF NOT EXISTS printers(
            id TEXT PRIMARY KEY,
            lock TEXT NOT NULL,
            lock_reason TEXT,
            queue_paused INTEGER NOT NULL DEFAULT 0,
            current_job INTEGER,
            last_completion REAL NOT NULL DEFAULT 0,
            busy_since REAL,
            cleared_by TEXT, cleared_at REAL);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, printer TEXT, msg TEXT);
        """)
        for pid in PRINTERS:
            c.execute("INSERT OR IGNORE INTO printers(id, lock, lock_reason) VALUES(?,?,?)",
                      (pid, "NEEDS_CLEAR", "Queue just started: check the plate is empty."))
        # Fail locked on restart: we don't know what happened while we were down.
        c.execute("UPDATE printers SET lock='NEEDS_CLEAR', "
                  "lock_reason='Queue restarted: check the plate is empty.' WHERE lock='READY'")


def event(c, pid, msg):
    log.info("[printer %s] %s", pid, msg)
    c.execute("INSERT INTO events(ts, printer, msg) VALUES(?,?,?)", (time.time(), pid, msg))


def set_lock(c, pid, lock, reason, **extra):
    sets = ["lock=?", "lock_reason=?"] + ["%s=?" % k for k in extra]
    c.execute("UPDATE printers SET %s WHERE id=?" % ", ".join(sets),
              [lock, reason] + list(extra.values()) + [pid])


def remove_upload(job):
    if job["stored"]:
        try:
            os.remove(os.path.join(UPLOAD_DIR, job["stored"]))
        except OSError:
            pass


# ---------------------------------------------------------------- poller

def finish_current(c, p, row, status, result):
    job = None
    if row["current_job"]:
        job = c.execute("SELECT * FROM jobs WHERE id=?", (row["current_job"],)).fetchone()
        c.execute("UPDATE jobs SET status=?, result=?, finished=? WHERE id=?",
                  (status, result, time.time(), row["current_job"]))
        if job:
            remove_upload(job)
            if job["octo_name"]:
                p.delete_file(job["octo_name"])
    c.execute("UPDATE printers SET current_job=NULL, busy_since=NULL, last_completion=0 "
              "WHERE id=?", (p.id,))
    return job


def who(job):
    return "%s's %s" % (job["student_name"], job["filename"]) if job else "A print not from the queue"


def poll_printer(p):
    st = p.status()
    start_job = None
    bad = st["kind"] in ("unreachable", "offline", "error")
    p.bad_polls = p.bad_polls + 1 if bad else 0
    # One slow or failed poll must not flash the page red: keep showing the last
    # good reading until the problem has lasted a few polls. Errors show at once.
    if st["kind"] == "error" or not bad or p.bad_polls >= BAD_POLLS_BEFORE_LOCK:
        LIVE[p.id] = st

    with tx() as c:
        row = c.execute("SELECT * FROM printers WHERE id=?", (p.id,)).fetchone()
        lock = row["lock"]

        if bad:
            if st["kind"] != "error" and p.bad_polls < BAD_POLLS_BEFORE_LOCK:
                return
            why = "%s: %s" % (st["text"], st["error"]) if st["error"] else st["text"]
            if st["kind"] == "unreachable":
                # We can't see OctoPrint (network or Pi problem). A running print
                # may well still be fine, so keep the job; just stop new starts.
                if lock == "READY":
                    event(c, p.id, "Locked: " + why)
                    notify("⚠️ **%s**: the queue can't reach OctoPrint (%s)." % (p.name, why),
                           staff=True)
                    set_lock(c, p.id, "NEEDS_CLEAR",
                             "The queue lost contact with this printer. Check the plate.")
                elif p.bad_polls == BAD_POLLS_BEFORE_LOCK:
                    event(c, p.id, "Lost contact with OctoPrint: " + why)
                return
            if lock == "BUSY":
                job = finish_current(c, p, row, "failed", "Printer stopped: " + why)
                event(c, p.id, "Print failed: " + why)
                notify("❌ **%s**: %s FAILED: %s. Staff please check the printer, "
                       "then clear the plate." % (p.name, who(job), why), staff=True)
                set_lock(c, p.id, "NEEDS_CLEAR",
                         "Printer had a problem during a print (%s). Fix it, then clear the plate." % why)
            elif lock == "READY":
                event(c, p.id, "Locked: " + why)
                notify("⚠️ **%s** is locked: %s" % (p.name, why), staff=True)
                set_lock(c, p.id, "NEEDS_CLEAR",
                         "Printer was offline or in error (%s). Check the plate." % why)
            return

        if st["kind"] == "busy":
            if lock != "BUSY":
                outside = not row["current_job"]
                event(c, p.id, "Print started outside the queue: %s" % st["file"] if outside
                      else "Printing")
                set_lock(c, p.id, "BUSY",
                         "Printing a file that was not sent by the queue" if outside else "Printing",
                         busy_since=time.time())
                if outside:
                    notify("ℹ️ **%s**: someone started `%s` directly in OctoPrint, "
                           "not through the queue." % (p.name, st["file"]))
            elif row["lock_reason"] == "Sending file to printer":
                c.execute("UPDATE printers SET lock_reason='Printing' WHERE id=?", (p.id,))
            if st["completion"] is not None:
                c.execute("UPDATE printers SET last_completion=MAX(last_completion, ?) WHERE id=?",
                          (st["completion"], p.id))
            return

        # Printer is connected and idle.
        if lock == "BUSY":
            if row["busy_since"] and time.time() - row["busy_since"] < START_GRACE \
                    and row["last_completion"] == 0:
                return   # we just sent a job; OctoPrint may not show "Printing" yet
            completion = max(row["last_completion"] or 0, st["completion"] or 0)
            if completion >= 99.5:
                job = finish_current(c, p, row, "done", "Finished")
                event(c, p.id, "Print finished")
                notify("✅ **%s**: %s is done. Please take it off the plate and press "
                       "**Plate is clear** so the next print can start." % (p.name, who(job)))
                set_lock(c, p.id, "NEEDS_CLEAR", "Print finished. Take it off the plate.")
            else:
                job = finish_current(c, p, row, "stopped", "Stopped at %.0f%%" % completion)
                event(c, p.id, "Print stopped at %.0f%%" % completion)
                notify("⏹️ **%s**: %s was stopped at %.0f%%. Clear the plate, then press "
                       "**Plate is clear**." % (p.name, who(job), completion))
                set_lock(c, p.id, "NEEDS_CLEAR",
                         "Print was stopped at %.0f%%. Remove what is left on the plate." % completion)
            return

        if lock == "READY" and not row["queue_paused"] and st["ready"]:
            start_job = c.execute("SELECT * FROM jobs WHERE printer=? AND status='queued' "
                                  "ORDER BY sort, id LIMIT 1", (p.id,)).fetchone()

    if start_job:
        launch(p, start_job)


def launch(p, job):
    octo_name = "q%d_%s" % (job["id"], secure_filename(job["filename"]) or "print.gcode")
    if not octo_name.lower().endswith(ALLOWED_EXT):
        octo_name += ".gcode"
    with tx() as c:   # mark BUSY first so nothing else starts meanwhile
        set_lock(c, p.id, "BUSY", "Sending file to printer", current_job=job["id"],
                 busy_since=time.time(), last_completion=0)
        c.execute("UPDATE jobs SET status='printing', started=?, octo_name=? WHERE id=?",
                  (time.time(), octo_name, job["id"]))
        event(c, p.id, "Starting job #%d (%s, %s)" % (job["id"], job["filename"], job["student_name"]))
    notify("▶️ **%s**: started %s (about %s)." % (p.name, who(job), fmt_dur(job["est_seconds"])))
    try:
        p.upload_and_print(os.path.join(UPLOAD_DIR, job["stored"]), octo_name)
    except Exception as e:   # noqa: BLE001 - any failure must lock the printer
        log.exception("upload failed")
        with tx() as c:
            c.execute("UPDATE jobs SET status='queued', started=NULL WHERE id=?", (job["id"],))
            c.execute("UPDATE printers SET current_job=NULL, busy_since=NULL WHERE id=?", (p.id,))
            set_lock(c, p.id, "NEEDS_CLEAR",
                     "Could not start job #%d (%s). Job kept in the queue." % (job["id"], e))
            event(c, p.id, "Start failed for job #%d: %s" % (job["id"], e))
        notify("❌ **%s**: could not start %s (%s). It stays in the queue." % (p.name, who(job), e),
               staff=True)


def poller():
    while True:
        for p in PRINTERS.values():
            try:
                poll_printer(p)
            except Exception:   # noqa: BLE001 - keep polling the other printer
                log.exception("poll failed for printer %s", p.id)
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- helpers

def estimate_seconds(path):
    """Read the slicer's time estimate from the G-code (Cura, PrusaSlicer, Orca)."""
    try:
        with open(path, "rb") as f:
            head = f.read(65536).decode("utf-8", "ignore")
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read().decode("utf-8", "ignore")
    except OSError:
        return None
    m = re.search(r";TIME:(\d+)", head)
    if m:
        return int(m.group(1))
    m = re.search(r"estimated printing time[^=]*=\s*([^\n\r]+)", head + tail)
    if m:
        total = 0
        for n, u in re.findall(r"(\d+)\s*([dhms])", m.group(1)):
            total += int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u]
        return total or None
    return None


def fmt_dur(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    if sec < 60:
        return "under 1m"
    h, m = sec // 3600, (sec % 3600) // 60
    return "%dh %02dm" % (h, m) if h else "%dm" % m


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%d %b %H:%M") if ts else "-"


def is_admin():
    return bool(session.get("admin"))


def my_jobs():
    return set(session.get("mine", []))


def view_data():
    out = []
    with tx() as c:
        for pid, p in PRINTERS.items():
            row = dict(c.execute("SELECT * FROM printers WHERE id=?", (pid,)).fetchone())
            cur = c.execute("SELECT * FROM jobs WHERE id=?", (row["current_job"],)).fetchone() \
                if row["current_job"] else None
            queue = c.execute("SELECT * FROM jobs WHERE printer=? AND status='queued' "
                              "ORDER BY sort, id", (pid,)).fetchall()
            recent = c.execute("SELECT * FROM jobs WHERE printer=? AND status IN "
                               "('done','stopped','failed') ORDER BY finished DESC LIMIT 5",
                               (pid,)).fetchall()
            # rough wait: what is left of the current print + queued estimates
            wait = LIVE[pid].get("time_left") or 0
            waits = []
            for j in queue:
                waits.append(wait)
                wait += (j["est_seconds"] or 0) + 300   # +5 min to swap prints
            out.append({"p": p, "row": row, "live": LIVE[pid], "cur": cur,
                        "queue": list(zip(queue, waits)), "recent": recent})
    return out


# ---------------------------------------------------------------- web app

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


def _secret():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(DATA_DIR, ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.secret_key = _secret()
app.jinja_env.globals.update(fmt_dur=fmt_dur, fmt_time=fmt_time, is_admin=is_admin)


@app.errorhandler(413)
def too_big(_e):
    flash("File is too big (limit %d MB)." % MAX_UPLOAD_MB, "bad")
    return redirect(url_for("index"))


@app.get("/")
def index():
    return render_template("index.html", printers=view_data(), mine=my_jobs(),
                           max_mb=MAX_UPLOAD_MB)


@app.get("/partial")
def partial():
    return render_template("_printers.html", printers=view_data(), mine=my_jobs())


@app.post("/submit")
def submit():
    pid = request.form.get("printer", "")
    name = request.form.get("name", "").strip()[:60]
    sid = request.form.get("student_id", "").strip()[:20]
    note = request.form.get("note", "").strip()[:200]
    f = request.files.get("file")
    if pid not in PRINTERS:
        flash("Pick a printer.", "bad")
    elif not name or not sid:
        flash("Enter your name and student ID.", "bad")
    elif not f or not f.filename:
        flash("Choose a G-code file.", "bad")
    elif not f.filename.lower().endswith(ALLOWED_EXT):
        flash("Only sliced G-code files (.gcode) can be printed. Slice your STL first.", "bad")
    else:
        stored = uuid.uuid4().hex + ".gcode"
        path = os.path.join(UPLOAD_DIR, stored)
        f.save(path)
        est = estimate_seconds(path)
        with tx() as c:
            cur = c.execute("INSERT INTO jobs(printer, student_name, student_id, note, filename, "
                            "stored, est_seconds, sort, created) VALUES(?,?,?,?,?,?,?,?,?)",
                            (pid, name, sid, note, f.filename[:120], stored, est,
                             time.time(), time.time()))
            jid = cur.lastrowid
            event(c, pid, "Job #%d queued by %s (%s): %s" % (jid, name, sid, f.filename))
        session["mine"] = list(my_jobs() | {jid})
        flash("Added to the queue for %s. Job #%d." % (PRINTERS[pid].name, jid), "ok")
    return redirect(url_for("index"))


def _job_or_404(jid):
    with tx() as c:
        j = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not j:
        abort(404)
    return j


@app.post("/job/<int:jid>/remove")
def remove_job(jid):
    j = _job_or_404(jid)
    if not (is_admin() or jid in my_jobs()):
        abort(403)
    if j["status"] == "queued":
        with tx() as c:
            c.execute("UPDATE jobs SET status='removed', finished=? WHERE id=?", (time.time(), jid))
            event(c, j["printer"], "Job #%d removed%s" % (jid, " by staff" if is_admin() else ""))
        remove_upload(j)
        flash("Job #%d removed from the queue." % jid, "ok")
    return redirect(request.referrer or url_for("index"))


@app.post("/job/<int:jid>/<cmd>")
def job_cmd(jid, cmd):
    """Cancel / pause / resume a print. Owner of the job or staff only."""
    j = _job_or_404(jid)
    if not (is_admin() or jid in my_jobs()):
        abort(403)
    p = PRINTERS[j["printer"]]
    payload = {"cancel": {"command": "cancel"},
               "pause": {"command": "pause", "action": "pause"},
               "resume": {"command": "pause", "action": "resume"}}.get(cmd)
    if not payload or j["status"] != "printing":
        abort(400)
    try:
        p.job_command(payload)
        with tx() as c:
            event(c, p.id, "Job #%d: %s pressed" % (jid, cmd))
        flash("Sent %s to %s." % (cmd, p.name), "ok")
    except requests.RequestException as e:
        flash("OctoPrint refused %s: %s" % (cmd, e), "bad")
    return redirect(request.referrer or url_for("index"))


@app.post("/printer/<pid>/clear")
def clear_plate(pid):
    p = PRINTERS.get(pid) or abort(404)
    who = request.form.get("who", "").strip()[:60]
    if not is_admin() and (not who or request.form.get("confirm") != "yes"):
        flash("Type your name and tick the box to confirm the plate is empty.", "bad")
        return redirect(request.referrer or url_for("index"))
    who = who or "staff"
    with tx() as c:
        row = c.execute("SELECT * FROM printers WHERE id=?", (pid,)).fetchone()
        if row["lock"] != "NEEDS_CLEAR":
            flash("%s does not need clearing right now." % p.name, "bad")
        elif LIVE[pid].get("kind") != "idle" and not is_admin():
            flash("%s is not ready (%s). Ask lab staff." % (p.name, LIVE[pid].get("text")), "bad")
        else:
            set_lock(c, pid, "READY", "Plate confirmed empty by %s" % who,
                     cleared_by=who, cleared_at=time.time())
            event(c, pid, "Plate marked clear by %s" % who)
            flash("Thanks! %s is unlocked. The next job will start by itself." % p.name, "ok")
    return redirect(request.referrer or url_for("index"))


@app.get("/printer/<pid>/snapshot.jpg")
def snapshot(pid):
    p = PRINTERS.get(pid) or abort(404)
    if not p.snapshot:
        abort(404)
    try:
        r = requests.get(p.snapshot, timeout=5)
        return Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"),
                        headers={"Cache-Control": "no-store"})
    except requests.RequestException:
        abort(502)


# ---------------------------------------------------------------- staff pages

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        if ADMIN_PASSWORD and secrets.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session["admin"] = True
        else:
            flash("Wrong password.", "bad")
        return redirect(url_for("admin"))
    if not is_admin():
        return render_template("login.html", configured=bool(ADMIN_PASSWORD))
    with tx() as c:
        events = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 150").fetchall()
    return render_template("admin.html", printers=view_data(), events=events, mine=set())


@app.post("/admin/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


@app.post("/admin/printer/<pid>/<action>")
def admin_printer(pid, action):
    if not is_admin():
        abort(403)
    if pid not in PRINTERS:
        abort(404)
    with tx() as c:
        if action == "pause_queue":
            c.execute("UPDATE printers SET queue_paused=1 WHERE id=?", (pid,))
            event(c, pid, "Queue paused by staff")
        elif action == "resume_queue":
            c.execute("UPDATE printers SET queue_paused=0 WHERE id=?", (pid,))
            event(c, pid, "Queue resumed by staff")
        elif action == "lock":
            row = c.execute("SELECT lock FROM printers WHERE id=?", (pid,)).fetchone()
            if row["lock"] == "READY":
                set_lock(c, pid, "NEEDS_CLEAR", "Locked by staff")
                event(c, pid, "Locked by staff")
        else:
            abort(400)
    return redirect(url_for("admin"))


@app.post("/admin/job/<int:jid>/move/<direction>")
def admin_move(jid, direction):
    if not is_admin():
        abort(403)
    j = _job_or_404(jid)
    with tx() as c:
        q = c.execute("SELECT id, sort FROM jobs WHERE printer=? AND status='queued' "
                      "ORDER BY sort, id", (j["printer"],)).fetchall()
        ids = [r["id"] for r in q]
        if jid in ids:
            i = ids.index(jid)
            k = i - 1 if direction == "up" else i + 1
            if 0 <= k < len(ids):
                ids[i], ids[k] = ids[k], ids[i]
                for n, x in enumerate(ids):
                    c.execute("UPDATE jobs SET sort=? WHERE id=?", (n, x))
    return redirect(url_for("admin"))


def main():
    init_db()
    if not PRINTERS:
        raise SystemExit("No printers configured. Copy config.example.env to config.env.")
    threading.Thread(target=poller, daemon=True).start()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    try:
        from waitress import serve
        log.info("Serving on http://%s:%d", host, port)
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
