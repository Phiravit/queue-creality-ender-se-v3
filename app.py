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
import hashlib
import hmac
import os
import re
import sqlite3
import secrets
import threading
import time
import uuid
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

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
# Discord servers to alert: DISCORD1_WEBHOOK_URL, DISCORD2_..., each with its own optional
# staff role (roles belong to one server). DISCORD_WEBHOOK_URL alone still works too.
HOOKS = []
for _n in [""] + [str(i) for i in range(1, 10)]:
    _url = os.environ.get("DISCORD%s_WEBHOOK_URL" % _n, "").strip().strip("()<>\"' ")
    if _url and _url not in [h["url"] for h in HOOKS]:
        HOOKS.append({"url": _url, "role": os.environ.get("DISCORD%s_STAFF_ROLE_ID" % _n, "").strip(),
                      # DISCORDn_PRINTERS=2 -> only printer 2's alerts and board (empty = all)
                      "printers": [x.strip() for x in os.environ.get("DISCORD%s_PRINTERS" % _n, "")
                                   .split(",") if x.strip()],
                      "key": hashlib.sha256(_url.encode()).hexdigest()[:12]})
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
# One Discord message showing every printer's status and queue, edited in place.
DISCORD_BOARD = os.environ.get("DISCORD_STATUS_BOARD", "1").strip().lower() not in ("0", "no", "off", "false")
BOARD_MIN_EDIT = float(os.environ.get("DISCORD_BOARD_SECONDS", "20"))   # at most one edit per this
# Clock times (finish estimates, history) are shown in this time zone. Thailand = +7 (no DST).
TZ_HOURS = float(os.environ.get("TIMEZONE_OFFSET_HOURS", "7"))
LOCAL_TZ = timezone(timedelta(hours=TZ_HOURS))
TZ_LABEL = "GMT%+g" % TZ_HOURS
MAX_BOOK_DAYS = float(os.environ.get("MAX_BOOK_DAYS", "7"))   # how far ahead a print can be timed
SWAP = 300   # rough minutes between prints: someone has to clear the plate
# Students sign in with their student ID (and their full name the first time).
STUDENT_ID_RE = re.compile(os.environ.get("STUDENT_ID_PATTERN", r"^[0-9]{6,15}$"))

os.makedirs(UPLOAD_DIR, exist_ok=True)


def notify(pid, text, staff=False):
    """Post to every Discord webhook that follows this printer, in the background. Never raises."""
    if PUBLIC_URL:
        text += "\n" + PUBLIC_URL + "/?p=" + pid
    for hook in HOOKS:
        if hook["printers"] and pid not in hook["printers"]:
            continue
        payload = {"content": text[:1900], "allowed_mentions": {"parse": []}}
        if staff and hook["role"]:
            payload["content"] = "<@&%s> %s" % (hook["role"], payload["content"])
            payload["allowed_mentions"]["roles"] = [hook["role"]]

        def send(url=hook["url"], payload=payload):
            try:
                requests.post(url, json=payload, timeout=10).raise_for_status()
            except requests.RequestException as e:
                log.warning("Discord webhook failed: %s", getattr(e.response, "status_code", e))
        threading.Thread(target=send, daemon=True).start()


def _webhook_url(hook_url, path="", **query):
    """A webhook URL with an extra path (e.g. /messages/<id>) and query, keeping any ?thread_id."""
    base, _, q = hook_url.partition("?")
    parts = [x for x in [q] + ["%s=%s" % kv for kv in query.items()] if x]
    return base.rstrip("/") + path + ("?" + "&".join(parts) if parts else "")


class Printer:
    def __init__(self, pid, name, url, key, snapshot):
        self.id = pid
        self.name = name
        self.url = url.rstrip("/")
        self.key = key
        self.snapshot = snapshot
        self.bad_polls = 0      # consecutive polls with any problem
        self.unreach_polls = 0  # consecutive polls where OctoPrint did not answer

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
            log.warning("printer %s: no answer from OctoPrint (%s)", self.id, st["error"])
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
        # Optional: students need this password to queue on the printer (staff don't).
        PRINTERS[pid].password = os.environ.get("PRINTER%d_PASSWORD" % i, "")

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
        CREATE TABLE IF NOT EXISTS students(
            student_id TEXT PRIMARY KEY, name TEXT NOT NULL, first_seen REAL, last_seen REAL);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS unlocks(            -- who already typed a printer's password
            student_id TEXT NOT NULL, printer TEXT NOT NULL, token TEXT NOT NULL,
            PRIMARY KEY(student_id, printer));
        """)
        # added later: the earliest time a job may start (NULL = as soon as possible)
        if "start_after" not in [r[1] for r in c.execute("PRAGMA table_info(jobs)")]:
            c.execute("ALTER TABLE jobs ADD COLUMN start_after REAL")
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
    p.unreach_polls = p.unreach_polls + 1 if st["kind"] == "unreachable" else 0
    # A single request that gets no answer must not replace what the page shows
    # (including a real printer error): only show "can't reach" once it lasts.
    if st["kind"] != "unreachable" or p.unreach_polls >= BAD_POLLS_BEFORE_LOCK:
        LIVE[p.id] = st

    with tx() as c:
        row = c.execute("SELECT * FROM printers WHERE id=?", (p.id,)).fetchone()
        lock = row["lock"]

        if bad:
            if st["kind"] == "unreachable" and p.unreach_polls < BAD_POLLS_BEFORE_LOCK:
                return
            if st["kind"] == "offline" and p.bad_polls < BAD_POLLS_BEFORE_LOCK:
                return
            why = "%s: %s" % (st["text"], st["error"]) if st["error"] else st["text"]
            if st["kind"] == "unreachable":
                # We can't see OctoPrint (network or Pi problem). A running print
                # may well still be fine, so keep the job; just stop new starts.
                if lock == "READY":
                    event(c, p.id, "Locked: " + why)
                    notify(p.id, "⚠️ **%s**: the queue can't reach OctoPrint (%s)." % (p.name, why),
                           staff=True)
                    set_lock(c, p.id, "NEEDS_CLEAR",
                             "The queue lost contact with this printer. Check the plate.")
                elif p.unreach_polls == BAD_POLLS_BEFORE_LOCK:
                    event(c, p.id, "Lost contact with OctoPrint: " + why)
                return
            if lock == "BUSY":
                job = finish_current(c, p, row, "failed", "Printer stopped: " + why)
                event(c, p.id, "Print failed: " + why)
                notify(p.id, "❌ **%s**: %s FAILED: %s. Staff please check the printer, "
                       "then clear the plate." % (p.name, who(job), why), staff=True)
                set_lock(c, p.id, "NEEDS_CLEAR",
                         "Printer had a problem during a print (%s). Fix it, then clear the plate." % why)
            elif lock == "READY":
                event(c, p.id, "Locked: " + why)
                notify(p.id, "⚠️ **%s** is locked: %s" % (p.name, why), staff=True)
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
                    notify(p.id, "ℹ️ **%s**: someone started `%s` directly in OctoPrint, "
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
                notify(p.id, "✅ **%s**: %s is done. Please take it off the plate and press "
                       "**Plate is clear** so the next print can start.\n%s"
                       % (p.name, who(job), next_up(c, p.id)))
                set_lock(c, p.id, "NEEDS_CLEAR", "Print finished. Take it off the plate.")
            else:
                job = finish_current(c, p, row, "stopped", "Stopped at %.0f%%" % completion)
                event(c, p.id, "Print stopped at %.0f%%" % completion)
                notify(p.id, "⏹️ **%s**: %s was stopped at %.0f%%. Clear the plate, then press "
                       "**Plate is clear**.\n%s" % (p.name, who(job), completion, next_up(c, p.id)))
                set_lock(c, p.id, "NEEDS_CLEAR",
                         "Print was stopped at %.0f%%. Remove what is left on the plate." % completion)
            return

        if lock == "READY" and not row["queue_paused"] and st["ready"]:
            # first in line whose start time has come; later timed jobs don't hold anyone up
            start_job = c.execute("SELECT * FROM jobs WHERE printer=? AND status='queued' "
                                  "AND (start_after IS NULL OR start_after<=?) "
                                  "ORDER BY sort, id LIMIT 1", (p.id, time.time())).fetchone()

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
    notify(p.id, "▶️ **%s**: started %s (about %s%s)." % (
        p.name, who(job), fmt_dur(job["est_seconds"]),
        ", done around %s %s" % (fmt_clock(time.time() + job["est_seconds"]), TZ_LABEL)
        if job["est_seconds"] else ""))
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
        notify(p.id, "❌ **%s**: could not start %s (%s). It stays in the queue." % (p.name, who(job), e),
               staff=True)


def next_up(c, pid):
    jobs = c.execute("SELECT * FROM jobs WHERE printer=? AND status='queued' ORDER BY sort, id",
                     (pid,)).fetchall()
    if not jobs:
        return "Nobody is waiting for this printer."
    j, _wait, _done = plan(jobs, time.time())[0]
    later = j["start_after"] and j["start_after"] > time.time() + 60
    return "Next up: %s%s%s." % (who(j), " (timed for %s %s)" % (fmt_clock(j["start_after"], 60), TZ_LABEL)
                                 if later else "",
                                 " (+%d more waiting)" % (len(jobs) - 1) if len(jobs) > 1 else "")


def plan(jobs, free_at):
    """Walk the clock through the queue the way the poller will: at each moment the
    first job in line whose start time has come goes next; if none has, skip ahead to
    the earliest start time. Returns (job, start, estimated finish or None) in print order."""
    pending, t, out = list(jobs), free_at, []
    while pending:
        due = [j for j in pending if not j["start_after"] or j["start_after"] <= t]
        if not due:
            t = min(j["start_after"] for j in pending)
            continue
        j = due[0]
        pending = [x for x in pending if x["id"] != j["id"]]
        out.append((j, t, t + j["est_seconds"] if j["est_seconds"] else None))
        t += (j["est_seconds"] or 0) + SWAP
    return out


def poller():
    while True:
        for p in PRINTERS.values():
            try:
                poll_printer(p)
            except Exception:   # noqa: BLE001 - keep polling the other printer
                log.exception("poll failed for printer %s", p.id)
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- Discord status board

BOARD_COLOURS = {"ok": 0x1F9D4D, "busy": 0x2B63C6, "warn": 0xD9750F, "bad": 0xD93A2B, "idle": 0x8A909C}
BOARD_ICONS = {"ok": "🟢", "busy": "🔵", "warn": "🟠", "bad": "🔴", "idle": "⚪"}


def board_payload():
    """The status message: one embed per printer with its state and queue."""
    embeds = []
    for d in view_data():
        p, row, live, cur = d["p"], d["row"], d["live"], d["cur"]
        colour, text = d["summary"]
        free_text, free_ts = d["free"]
        lines = ["%s **%s**" % (BOARD_ICONS[colour], text),
                 "🕒 %s%s" % (free_text, " (%s)" % TZ_LABEL if free_ts else "")]
        if row["lock"] == "BUSY":
            lines.append("%s · `%s`" % (cur["student_name"] if cur else "Not from the queue",
                                        cur["filename"] if cur else live.get("file") or "?"))
            lines.append(finish_line(d["cur_done"]))
        elif row["lock"] == "NEEDS_CLEAR" or colour == "bad":
            lines.append(live.get("error") or live.get("text") if colour == "bad" else row["lock_reason"])
        q = d["queue"]
        qtext = "\n".join("%d. %s · `%s` (%s%s)" % (
            i + 1, j["student_name"], j["filename"],
            "⏰ starts ~%s, " % fmt_clock(time.time() + w, 60) if j["start_after"] and w > 60 else "",
            fmt_dur(j["est_seconds"]) + (", done ~%s" % fmt_clock(done) if done else ""))
            for i, (j, w, done) in enumerate(q[:10])) or "Nobody is waiting."
        if len(q) > 10:
            qtext += "\n…and %d more" % (len(q) - 10)
        e = {"title": p.name[:250], "color": BOARD_COLOURS[colour],
             "description": "\n".join(x for x in lines if x)[:2000],
             "fields": [{"name": "Queue (%d)" % len(q), "value": qtext[:1000]}]}
        if PUBLIC_URL:
            e["url"] = PUBLIC_URL + "/?p=" + p.id
        embeds.append(e)
    return {"content": "**3D printer status** · this message updates by itself",
            "embeds": embeds, "allowed_mentions": {"parse": []}}


def _meta(key, value=None):
    with tx() as c:
        if value is None:
            r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return r["value"] if r else None
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value))


def board_send(hook, payload):
    """Edit this server's status message, or post a new one if it was never sent or got deleted."""
    key = "board_msg:" + hook["key"]
    mid = _meta(key)
    if mid:
        r = requests.patch(_webhook_url(hook["url"], "/messages/" + mid), json=payload, timeout=10)
        if r.status_code != 404:
            r.raise_for_status()
            return
    r = requests.post(_webhook_url(hook["url"], wait="true"), json=payload, timeout=10)
    r.raise_for_status()
    _meta(key, str(r.json()["id"]))


def board_loop():
    """Keep one status message per Discord server up to date. One server failing
    (deleted webhook, rate limit) never holds up the others or the queue."""
    state = {h["key"]: {"sig": None, "sent": 0.0} for h in HOOKS}
    while True:
        time.sleep(max(POLL_SECONDS, 1))
        try:
            payload = board_payload()
        except Exception:   # noqa: BLE001
            log.warning("could not build the Discord status board", exc_info=True)
            continue
        now = time.time()
        for hook in HOOKS:
            # this server's printers only (embeds come in PRINTERS order)
            embeds = [dict(e) for pid, e in zip(PRINTERS, payload["embeds"])
                      if not hook["printers"] or pid in hook["printers"]]
            if not embeds:
                continue
            s, sig = state[hook["key"]], repr(embeds)
            if not ((sig != s["sig"] and now - s["sent"] >= BOARD_MIN_EDIT) or now - s["sent"] >= 300):
                continue
            p = dict(payload, embeds=embeds)
            p["embeds"][-1]["timestamp"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
            p["embeds"][-1]["footer"] = {"text": "Last updated"}
            try:
                board_send(hook, p)
                s["sig"] = sig
            except Exception as e:   # noqa: BLE001 - Discord trouble must never stop the queue
                log.warning("Discord status board update failed: %s", getattr(getattr(e, "response", None),
                                                                              "status_code", e))
            s["sent"] = now      # also backs off after a failure


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
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%d %b %H:%M") if ts else "-"


def finish_line(done_ts):
    """'Another 10m · estimated finish 13:46 (GMT+7)' for a running print."""
    if not done_ts:
        return "Estimated finish: working it out…"
    return "Another %s · estimated finish %s (%s)" % (
        fmt_dur(max(0, done_ts - time.time())), fmt_clock(done_ts, 60), TZ_LABEL)


def fmt_clock(ts, step=300):
    """Clock time for an estimate, rounded to `step` seconds (5 min by default):
    '15:40', 'tomorrow 09:05'."""
    if not ts:
        return "-"
    dt = datetime.fromtimestamp(round(ts / step) * step, LOCAL_TZ)
    days = (dt.date() - datetime.now(LOCAL_TZ).date()).days
    if days == 0:
        return dt.strftime("%H:%M")
    if days == 1:
        return dt.strftime("tomorrow %H:%M")
    return dt.strftime("%a %d %b %H:%M")


def is_admin():
    return bool(session.get("admin"))


def user_sid():
    return session.get("sid")


def owns(job):
    return bool(user_sid()) and job["student_id"] == user_sid()


def my_jobs():
    """Ids of this student's queued or printing jobs (on any device)."""
    if not user_sid():
        return set()
    with tx() as c:
        return {r["id"] for r in c.execute(
            "SELECT id FROM jobs WHERE student_id=? AND status IN ('queued','printing')",
            (user_sid(),))}


def _pw_token(p):
    return hmac.new(app.secret_key.encode(), ("%s:%s" % (p.id, p.password)).encode(),
                    hashlib.sha256).hexdigest()[:24]


def printer_unlocked(p):
    """No password, staff, or this student already typed the current password (any device).
    Changing the password changes the token, so everyone is asked again."""
    if not p.password or is_admin():
        return True
    if not user_sid():
        return False
    with tx() as c:
        r = c.execute("SELECT token FROM unlocks WHERE student_id=? AND printer=?",
                      (user_sid(), p.id)).fetchone()
    return bool(r) and hmac.compare_digest(r["token"], _pw_token(p))


def remember_unlock(p):
    if p.password and user_sid():
        with tx() as c:
            c.execute("INSERT OR REPLACE INTO unlocks(student_id, printer, token) VALUES(?,?,?)",
                      (user_sid(), p.id, _pw_token(p)))


def free_info(row, live, cur_done, rows):
    """When the printer is expected to be free: (short text, clock time or None)."""
    if live.get("kind") in ("error", "offline"):
        return "Out of service until staff fix it", None
    if row["queue_paused"] and rows:
        return "Queue paused by staff", None
    if rows and rows[0][1] > 600 and row["lock"] != "BUSY":
        # only timed prints are waiting, and the first one is a while off
        first = time.time() + rows[0][1]
        return "%s until ~%s, when a timed print starts" % (
            "Free now" if row["lock"] == "READY" else "Free once the plate is cleared",
            fmt_clock(first, 60)), first
    if rows:
        last = rows[-1][2]
        if last and all(r[2] for r in rows):
            return "Free by ~%s" % fmt_clock(last), last
        return "Free time unknown (a file has no time estimate)", None
    if row["lock"] == "BUSY":
        return ("Free by ~%s" % fmt_clock(cur_done), cur_done) if cur_done else ("Free after this print", None)
    if row["lock"] == "NEEDS_CLEAR":
        return "Free once the plate is cleared", None
    if live.get("kind") == "unreachable":
        return "Free time unknown (can't reach the printer)", None
    return "Free now", None


def summary(row, live):
    """Short status for a printer tab: (colour, text)."""
    if live.get("kind") == "error":
        return "bad", "Error"
    if live.get("kind") == "offline":
        return "bad", "Offline"
    if live.get("kind") == "unreachable":
        return "bad", "Can't reach"
    if row["lock"] == "BUSY":
        return "busy", ("Paused" if live.get("paused") else
                        "Printing %.0f%%" % (live.get("completion") or 0))
    if row["lock"] == "NEEDS_CLEAR":
        return "warn", "Clear the plate"
    if row["queue_paused"]:
        return "idle", "Queue paused"
    return "ok", "Ready"


def selected_printer():
    """The printer tab to show: ?p= in the URL, else the last one picked, else the first."""
    pid = request.args.get("p") or session.get("printer")
    if pid not in PRINTERS:
        pid = next(iter(PRINTERS))
    session["printer"] = pid
    return pid


def view_data(only=None):
    out = []
    with tx() as c:
        for pid, p in PRINTERS.items():
            if only and pid != only:
                continue
            row = dict(c.execute("SELECT * FROM printers WHERE id=?", (pid,)).fetchone())
            cur = c.execute("SELECT * FROM jobs WHERE id=?", (row["current_job"],)).fetchone() \
                if row["current_job"] else None
            queue = c.execute("SELECT * FROM jobs WHERE printer=? AND status='queued' "
                              "ORDER BY sort, id", (pid,)).fetchall()
            recent = c.execute("SELECT * FROM jobs WHERE printer=? AND status IN "
                               "('done','stopped','failed') ORDER BY finished DESC LIMIT 5",
                               (pid,)).fetchall()
            # rough wait: what is left of the current print + queued estimates
            now, live = time.time(), LIVE[pid]
            left = live.get("time_left")
            if row["lock"] == "BUSY" and not left:
                # OctoPrint gave no estimate: use the speed so far, else the slicer's estimate
                done_pct, spent = live.get("completion") or 0, live.get("print_time") or 0
                if done_pct >= 2 and spent:
                    left = spent * (100 - done_pct) / done_pct
                elif cur and cur["est_seconds"] and cur["started"]:
                    left = max(60, cur["started"] + cur["est_seconds"] - now)
            left = left if row["lock"] == "BUSY" else None
            # each queued job, in the order it will print:
            # (job, seconds until it starts, estimated finish time or None)
            rows = [(j, start - now, done) for j, start, done in plan(queue, now + (left or 0))]
            cur_done = now + left if left else None
            out.append({"p": p, "row": row, "live": live, "cur": cur,
                        "queue": rows, "recent": recent, "cur_done": cur_done,
                        "free": free_info(row, live, cur_done, rows),
                        "summary": summary(row, live)})
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
app.permanent_session_lifetime = timedelta(days=120)   # stay signed in for a semester
app.jinja_env.globals.update(fmt_dur=fmt_dur, fmt_time=fmt_time, fmt_clock=fmt_clock, finish_line=finish_line, tz_label=TZ_LABEL, is_admin=is_admin,
                             user_sid=user_sid, user_name=lambda: session.get("name"),
                             printer_unlocked=printer_unlocked)

@app.context_processor
def _now():
    return {"now": time.time()}


OPEN_ENDPOINTS = {"login", "admin", "logout", "static"}


@app.before_request
def require_login():
    if request.endpoint in OPEN_ENDPOINTS or user_sid() or is_admin():
        return None
    if request.endpoint == "partial":
        return "", 401          # the page script reloads and lands on the login page
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        sid = "".join(request.form.get("sid", "").split())[:20]
        name = " ".join(request.form.get("name", "").split())[:60]
        with tx() as c:
            known = c.execute("SELECT name FROM students WHERE student_id=?", (sid,)).fetchone() \
                if STUDENT_ID_RE.match(sid) else None
            if not STUDENT_ID_RE.match(sid):
                flash("Enter your student ID (numbers only).", "bad")
            elif not name and not known:
                flash("First time here: also enter your full name.", "bad")
            else:
                name = name or known["name"]
                now = time.time()
                c.execute("INSERT INTO students(student_id, name, first_seen, last_seen) "
                          "VALUES(?,?,?,?) ON CONFLICT(student_id) DO UPDATE SET "
                          "name=excluded.name, last_seen=excluded.last_seen",
                          (sid, name, now, now))
                session.permanent = True
                session["sid"], session["name"] = sid, name
                return redirect(url_for("index"))
        return render_template("student_login.html", sid=sid, name=name)
    return render_template("student_login.html", sid="", name="")


@app.errorhandler(413)
def too_big(_e):
    flash("File is too big (limit %d MB)." % MAX_UPLOAD_MB, "bad")
    return redirect(url_for("index"))


@app.get("/")
def index():
    pid = selected_printer()
    printers = view_data()
    lab_now = datetime.now(LOCAL_TZ).replace(second=0, microsecond=0)
    pick = (lab_now + timedelta(hours=1)).replace(minute=0)   # date picker starts at the next full hour
    if pick <= lab_now:
        pick += timedelta(hours=1)
    fmt = "%Y-%m-%dT%H:%M"
    return render_template("index.html", printers=printers, pid=pid, p=PRINTERS[pid],
                           shown=[d for d in printers if d["p"].id == pid],
                           mine=my_jobs(), max_mb=MAX_UPLOAD_MB, max_days=MAX_BOOK_DAYS,
                           tz_hours=TZ_HOURS, book_min=lab_now.strftime(fmt),
                           book_max=(lab_now + timedelta(days=MAX_BOOK_DAYS)).strftime(fmt),
                           book_default=pick.strftime(fmt))


@app.get("/partial")
def partial():
    """Live parts of the page: ?part=tabs the tab bar, ?p= one printer's card,
    neither: every printer (staff page)."""
    pid = request.args.get("p")
    if request.args.get("part") == "tabs" and pid in PRINTERS:
        return render_template("_tabs.html", printers=view_data(), pid=pid)
    return render_template("_printers.html", mine=my_jobs(),
                           printers=view_data(pid if pid in PRINTERS else None))


def parse_start(form):
    """'When should it print?': (earliest start time or None for now, error text or None).
    The timer counts from the server's clock; the date picker is read as lab time (GMT+7)."""
    when, now = form.get("when", "now"), time.time()
    if when == "in":
        try:
            hours = float(form.get("in_hours", "").replace(",", "."))
        except ValueError:
            return None, "Type how many hours from now, e.g. 1 or 2.5."
        if not 0 <= hours <= MAX_BOOK_DAYS * 24:
            return None, "Pick a time within the next %g days." % MAX_BOOK_DAYS
        start = now + hours * 3600
    elif when == "at":
        try:
            start = datetime.strptime(form.get("at_time", ""), "%Y-%m-%dT%H:%M") \
                .replace(tzinfo=LOCAL_TZ).timestamp()
        except ValueError:
            return None, "Pick a date and time."
        if start < now - 120:
            return None, "That time has already passed. Pick a later one, or choose 'Right away'."
        if start > now + MAX_BOOK_DAYS * 86400:
            return None, "Pick a time within the next %g days." % MAX_BOOK_DAYS
    else:
        return None, None
    return (start if start > now + 60 else None), None


@app.post("/submit")
def submit():
    pid = request.form.get("printer", "")
    name, sid = session.get("name") or "Staff", user_sid() or "staff"
    note = request.form.get("note", "").strip()[:200]
    f = request.files.get("file")
    start_after, when_err = parse_start(request.form)
    if pid not in PRINTERS:
        flash("Pick a printer.", "bad")
    elif when_err:
        flash(when_err, "bad")
    elif not f or not f.filename:
        flash("Choose a G-code file.", "bad")
    elif not f.filename.lower().endswith(ALLOWED_EXT):
        flash("Only sliced G-code files (.gcode) can be printed. Slice your STL first.", "bad")
    elif not printer_unlocked(PRINTERS[pid]) and not hmac.compare_digest(
            request.form.get("printer_password", "").encode(), PRINTERS[pid].password.encode()):
        flash("Wrong password for %s. Ask lab staff for it." % PRINTERS[pid].name, "bad")
    else:
        remember_unlock(PRINTERS[pid])      # never ask this student again (until it changes)
        stored = uuid.uuid4().hex + ".gcode"
        path = os.path.join(UPLOAD_DIR, stored)
        f.save(path)
        est = estimate_seconds(path)
        with tx() as c:
            cur = c.execute("INSERT INTO jobs(printer, student_name, student_id, note, "
                            "filename, stored, est_seconds, sort, created, start_after) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (pid, name, sid, note, f.filename[:120], stored, est,
                             time.time(), time.time(), start_after))
            jid = cur.lastrowid
            event(c, pid, "Job #%d queued by %s (%s): %s%s" % (
                jid, name, sid, f.filename,
                ", start at %s" % fmt_time(start_after) if start_after else ""))
        d = view_data(pid)[0]
        order = [x[0]["id"] for x in d["queue"]]
        wait, done = next((x[1], x[2]) for x in d["queue"] if x[0]["id"] == jid)
        ahead = order.index(jid) + (d["row"]["lock"] == "BUSY")
        timed = "timed for %s, " % fmt_clock(start_after, 60) if start_after else ""
        notify(pid, "📥 **%s**: %s joined the queue (%s%s, about %s%s)." % (
            PRINTERS[pid].name, "%s's `%s`" % (name, f.filename[:120]), timed,
            "%d ahead" % ahead if ahead else "first in line", fmt_dur(est),
            ", done around %s %s" % (fmt_clock(done), TZ_LABEL) if done else ""))
        msg = "Added to the queue for %s (job #%d)." % (PRINTERS[pid].name, jid)
        if done:
            msg += (" It should start around %s and be done around %s (%s): pick it up then."
                    " Rough times: it waits if the plate isn't cleared." % (
                        fmt_clock(time.time() + wait, 60), fmt_clock(done), TZ_LABEL))
        elif start_after:
            msg += " It starts around %s (%s) at the earliest. The file has no time estimate, " \
                   "so there's no finish time." % (fmt_clock(time.time() + wait, 60), TZ_LABEL)
        flash(msg, "ok")
    return redirect(url_for("index", p=pid) if pid in PRINTERS else url_for("index"))


def _job_or_404(jid):
    with tx() as c:
        j = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not j:
        abort(404)
    return j


@app.post("/job/<int:jid>/remove")
def remove_job(jid):
    j = _job_or_404(jid)
    if not (is_admin() or owns(j)):
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
    if not (is_admin() or owns(j)):
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
    if not is_admin() and request.form.get("confirm") != "yes":
        flash("Tick the box to confirm the plate is empty.", "bad")
        return redirect(request.referrer or url_for("index"))
    who = "%s (%s)" % (session["name"], user_sid()) if user_sid() else "staff"
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
    return render_template("admin.html", printers=view_data(), events=events, mine=my_jobs())


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
    if HOOKS and DISCORD_BOARD:
        log.info("Discord: %d server(s), status board on", len(HOOKS))
        threading.Thread(target=board_loop, daemon=True).start()
    host =os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    try:
        from waitress import serve
        log.info("Serving on http://%s:%d", host, port)
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
