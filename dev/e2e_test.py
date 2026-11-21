"""End-to-end test: real queue app + two fake OctoPrints. No real printers.

    python dev/e2e_test.py
"""
import os
import subprocess
import sys
import tempfile
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "app.py")
Q = "http://127.0.0.1:8765"
F1, F2 = "http://127.0.0.1:5101", "http://127.0.0.1:5102"
procs = []


def start(args, env=None, log=None):
    out = open(log, "w") if log else subprocess.DEVNULL
    p = subprocess.Popen([sys.executable] + args, env=env, stdout=out, stderr=subprocess.STDOUT)
    procs.append(p)


def wait_until(fn, what, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return
        time.sleep(0.3)
    raise AssertionError("timed out waiting for: " + what)


def lock(pid):
    import sqlite3
    c = sqlite3.connect(os.path.join(data, "queue.db"))
    return c.execute("SELECT lock FROM printers WHERE id=?", (pid,)).fetchone()[0]


def job_status(jid):
    import sqlite3
    c = sqlite3.connect(os.path.join(data, "queue.db"))
    return c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0]


def gcode(name, minutes):
    path = os.path.join(data, name)
    with open(path, "w") as f:
        f.write(";FLAVOR:Marlin\n;TIME:%d\nG28\nG1 X10 Y10\n" % (minutes * 60))
    return path


def submit(s, printer, name, path):
    with open(path, "rb") as f:
        r = s.post(Q + "/submit", data={"printer": printer, "name": name, "student_id": "6500" + name[:3],
                                        "note": ""}, files={"file": (os.path.basename(path), f)})
    assert r.status_code == 200, r.status_code
    return r


def login(sid, name=""):
    s = requests.Session()
    s.post(Q + "/login", data={"sid": sid, "name": name})
    return s


def clear(s, printer, who="Tester"):
    return s.post(Q + "/printer/%s/clear" % printer, data={"who": who, "confirm": "yes"})


data = tempfile.mkdtemp(prefix="pq_")
env = dict(os.environ, QUEUE_CONFIG=os.path.join(data, "none.env"), DATA_DIR=data, PORT="8765",
           HOST="127.0.0.1", POLL_SECONDS="0.5", START_GRACE_SECONDS="3", ADMIN_PASSWORD="staffpw",
           PRINTER1_NAME="Free Use", PRINTER1_URL=F1, PRINTER1_APIKEY="KEY1",
           PRINTER2_NAME="Research", PRINTER2_URL=F2, PRINTER2_APIKEY="KEY2",
           DISCORD_WEBHOOK_URL=F1 + "/fake/webhook", DISCORD_STAFF_ROLE_ID="999", PUBLIC_URL="http://queue.test")
start([os.path.join(HERE, "fake_octoprint.py"), "5101", "KEY1"])
start([os.path.join(HERE, "fake_octoprint.py"), "5102", "KEY2"])
time.sleep(1.5)
start([APP], env=env, log=os.path.join(data, "app.log"))


def _up():
    try:
        return requests.get(Q + "/", timeout=1).ok
    except requests.RequestException:
        return False


ok = []
try:
    wait_until(_up, "queue web server")
    # 0. Sign-in: student ID (+ full name the first time); pages locked otherwise.
    anon = requests.Session()
    r = anon.get(Q + "/", allow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert anon.get(Q + "/partial").status_code == 401
    assert anon.post(Q + "/submit", data={"printer": "1"}, allow_redirects=False).status_code == 302
    for bad in ("", "abc", "12 ab", "1234", "x@kmutt.ac.th"):
        r = anon.post(Q + "/login", data={"sid": bad, "name": "X"})
        assert "Enter your student ID" in r.text, bad
        assert anon.get(Q + "/", allow_redirects=False).status_code == 302, bad
    r = anon.post(Q + "/login", data={"sid": "65130500001", "name": "  "})
    assert "First time here" in r.text
    assert anon.get(Q + "/", allow_redirects=False).status_code == 302
    alice = login("65130500001", "Alice  Wong")
    bob = login("65130500002", "Bob")
    carol = login("65130500003", "Carol")
    assert "Alice Wong" in alice.get(Q + "/").text and "65130500001" in alice.get(Q + "/").text
    ok.append("sign-in: student ID + name first time; bad IDs refused; pages locked otherwise")

    # 1. Fresh start is locked: a job must not start before someone clears the plate.
    submit(alice, "1", "Alice", gcode("alice.gcode", 90))
    time.sleep(2)
    assert lock("1") == "NEEDS_CLEAR" and job_status(1) == "queued"
    ok.append("starts locked; queued job waits for plate clear")

    # 2. Clearing starts the job by itself.
    submit(bob, "1", "Bob", gcode("bob.gcode", 30))
    clear(carol, "1")
    wait_until(lambda: job_status(1) == "printing" and lock("1") == "BUSY", "job 1 printing")
    assert job_status(2) == "queued"
    page = alice.get(Q + "/").text
    assert "Printing" in page, page[:3000]
    assert "30m" in page, "slicer estimate"
    assert "Cancel print" in page, "owner cancel"
    assert "Cancel print" not in carol.get(Q + "/").text, "other students must not see cancel"
    alice_phone = login(" 6513 0500001 ")      # returning student: ID only, name remembered
    assert "Alice Wong" in alice_phone.get(Q + "/").text, "name remembered from first sign-in"
    assert "Cancel print" in alice_phone.get(Q + "/").text, "same student ID on another device owns the job"
    ok.append("plate clear -> next job auto-starts; slicer time shown; only owner sees cancel")

    # 3. When the print finishes, the printer locks again and job 2 waits.
    wait_until(lambda: job_status(1) == "done", "job 1 done")
    wait_until(lambda: lock("1") == "NEEDS_CLEAR", "lock after finish")
    time.sleep(2)
    assert job_status(2) == "queued"
    assert "Waiting for the plate to be cleared" in carol.get(Q + "/").text
    ok.append("finished print locks printer; next job does not start on a full plate")

    # 4. Clear without ticking the box is refused.
    carol.post(Q + "/printer/1/clear", data={"who": "Carol"})
    assert lock("1") == "NEEDS_CLEAR"
    ok.append("clear without confirmation tick is refused")

    # 5. Bob's job starts; Bob cancels it -> 'stopped' + locked.
    clear(carol, "1")
    wait_until(lambda: job_status(2) == "printing", "job 2 printing")
    assert carol.post(Q + "/job/2/cancel").status_code == 403
    time.sleep(1)
    bob.post(Q + "/job/2/cancel")
    wait_until(lambda: job_status(2) == "stopped" and lock("1") == "NEEDS_CLEAR", "job 2 stopped")
    ok.append("owner can cancel, others get 403; cancelled print locks printer")

    # 6. Students can only remove their own queued job.
    submit(alice, "2", "Alice", gcode("alice2.gcode", 10))
    assert bob.post(Q + "/job/3/remove").status_code == 403
    alice.post(Q + "/job/3/remove")
    assert job_status(3) == "removed"
    ok.append("remove: own job only")

    # 7. A print started directly in OctoPrint is detected and locks after.
    clear(carol, "2")
    wait_until(lambda: lock("2") == "READY", "printer 2 ready")
    requests.post(F2 + "/fake/external")
    wait_until(lambda: lock("2") == "BUSY", "external print noticed")
    assert "Not from the queue" in carol.get(Q + "/").text
    wait_until(lambda: lock("2") == "NEEDS_CLEAR", "lock after external print")
    ok.append("print started outside the queue is detected and locks afterwards")

    # 8. Printer error mid-print: job failed, error shown, printer locked, clear refused.
    submit(alice, "2", "Alice", gcode("alice3.gcode", 10))
    requests.post(F2 + "/fake/duration/30.0")
    clear(carol, "2")
    wait_until(lambda: job_status(4) == "printing", "job 4 printing")
    time.sleep(1)
    requests.post(F2 + "/fake/error")
    wait_until(lambda: job_status(4) == "failed", "job 4 failed")
    page = carol.get(Q + "/").text
    assert "Printer error" in page and "Thermal Runaway" in page, page[page.find("Research"):][:1500]
    requests.post(F2 + "/fake/blackout/0.8")
    time.sleep(0.9)
    assert "Thermal Runaway" in carol.get(Q + "/").text, "a short outage must not hide a printer error"
    clear(carol, "2")
    assert lock("2") == "NEEDS_CLEAR", "students cannot clear a printer that is in error"
    ok.append("printer error: job failed, error shown, students cannot unlock")

    # 9. Staff login, pause queue, event log.
    staff = requests.Session()
    staff.post(Q + "/admin", data={"password": "wrong"})
    assert "Event log" not in staff.get(Q + "/admin").text
    assert staff.get(Q + "/", allow_redirects=False).status_code == 302, "wrong staff password = not signed in"
    staff.post(Q + "/admin", data={"password": "staffpw"})
    adm = staff.get(Q + "/admin").text
    assert "Event log" in adm and "Plate marked clear by Carol (65130500003)" in adm
    staff.post(Q + "/admin/printer/1/pause_queue")
    submit(alice, "1", "Alice", gcode("alice4.gcode", 5))
    clear(carol, "1")
    time.sleep(2)
    assert job_status(5) == "queued", "paused queue must not start jobs"
    staff.post(Q + "/admin/printer/1/resume_queue")
    wait_until(lambda: job_status(5) == "printing", "job 5 after resume")
    ok.append("staff login, pause/resume queue, event log")

    # 10. Uploaded files are cleaned up on OctoPrint after the job.
    wait_until(lambda: job_status(5) == "done", "job 5 done")
    left = requests.get(F1 + "/fake/files").json()
    assert not left, left
    ok.append("finished files deleted from OctoPrint storage")

    # 11. Losing contact mid-print keeps the job (no false FAILED, no "outside" print).
    requests.post(F1 + "/fake/duration/12.0")
    submit(alice, "1", "Alice", gcode("alice5.gcode", 5))
    clear(carol, "1")
    wait_until(lambda: job_status(6) == "printing" and lock("1") == "BUSY", "job 6 printing")
    time.sleep(1.5)
    requests.post(F1 + "/fake/blackout/4.0")
    time.sleep(0.6)
    assert "Can't reach this printer" not in carol.get(Q + "/").text, "one blip must not turn the page red"
    time.sleep(2.2)
    page = carol.get(Q + "/").text
    assert "Can't reach this printer" in page and "probably still running" in page
    assert job_status(6) == "printing" and lock("1") == "BUSY"
    wait_until(lambda: job_status(6) == "done", "job 6 done after contact came back", timeout=30)
    msgs = requests.get(F1 + "/fake/messages").json()
    assert not any("alice5" in m["content"] and "FAILED" in m["content"] for m in msgs)
    assert not any("directly in OctoPrint" in m["content"] and "q6_" in m["content"] for m in msgs)
    ok.append("lost contact mid-print: job kept, page says 'can't reach', finishes normally")

    # 12. Discord alerts went out, staff role pinged only on problems.
    msgs = requests.get(F1 + "/fake/messages").json()
    texts = [m["content"] for m in msgs]
    done = [t for t in texts if "Alice Wong's alice.gcode is done" in t]
    assert done and "Plate is clear" in done[0] and "http://queue.test" in done[0], texts
    failed = [m for m in msgs if "FAILED" in m["content"]]
    assert failed and failed[0]["content"].startswith("<@&999>") and failed[0]["allowed_mentions"]["roles"] == ["999"]
    assert not any(t.startswith("<@&") for t in texts if "is done" in t), "no staff ping on normal finish"
    assert any("directly in OctoPrint" in t for t in texts)
    assert any("was stopped at" in t for t in texts)
    ok.append("Discord alerts: finished / stopped / outside-queue / failed (+staff ping)")

    # 13. Wrong file type refused.
    r = alice.post(Q + "/submit", data={"printer": "1", "name": "A", "student_id": "1"},
                   files={"file": ("model.stl", b"solid x")})
    assert "Only sliced G-code" in r.text
    ok.append("non-G-code upload refused")
finally:
    for p in procs:
        p.terminate()
    for line in ok:
        print("PASS", line)
    if sys.exc_info()[0]:
        with open(os.path.join(data, "app.log")) as f:
            print("--- app log (last 25 lines) ---")
            print("".join(f.readlines()[-25:]))
    print("%d checks passed" % len(ok))
