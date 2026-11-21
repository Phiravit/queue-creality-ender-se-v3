"""A tiny fake OctoPrint for testing the queue without touching real printers.

    python dev/fake_octoprint.py 5101 KEY1

Test hooks (no key needed):
    POST /fake/external   start a print that did not come from the queue
    POST /fake/error      put the printer in an error state
    POST /fake/reset      back to idle Operational
    POST /fake/duration/<s>  how long a fake print takes (default 6 s)
"""
import sys
import time

from flask import Flask, abort, jsonify, request

app = Flask(__name__)
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5101
KEY = sys.argv[2] if len(sys.argv) > 2 else "testkey"
S = {"file": None, "start": 0, "elapsed_paused": 0, "paused_at": None, "printing": False,
     "error": None, "completion": None, "duration": 6.0, "files": set()}


BLACKOUT = {"until": 0}


def auth():
    if time.time() < BLACKOUT["until"]:
        abort(503)   # looks like OctoPrint/the Pi is unreachable
    k = request.headers.get("X-Api-Key") or request.headers.get("Authorization", "")[7:]
    if k != KEY:
        abort(403)


def tick():
    if S["printing"] and not S["paused_at"]:
        el = time.time() - S["start"] - S["elapsed_paused"]
        S["completion"] = min(100.0, el / S["duration"] * 100)
        if S["completion"] >= 100:
            S["printing"] = False


def state_text():
    if S["error"]:
        return "Error: " + S["error"]
    if S["printing"]:
        return "Paused" if S["paused_at"] else "Printing"
    return "Operational"


@app.get("/api/job")
def job():
    auth(); tick()
    el = None if S["completion"] is None else S["completion"] / 100 * S["duration"]
    out = {"state": state_text(),
           "job": {"file": {"name": S["file"], "display": S["file"], "origin": "local"}},
           "progress": {"completion": S["completion"], "printTime": el,
                        "printTimeLeft": None if el is None else max(0, S["duration"] - el)}}
    if S["error"]:
        out["error"] = S["error"]
    return jsonify(out)


@app.get("/api/printer")
def printer():
    auth(); tick()
    paused = bool(S["printing"] and S["paused_at"])
    flags = {"operational": not S["error"], "printing": S["printing"] and not paused,
             "paused": paused, "pausing": False, "cancelling": False, "sdReady": True,
             "error": bool(S["error"]), "ready": not S["error"] and not S["printing"],
             "closedOrError": bool(S["error"])}
    return jsonify({"state": {"text": state_text(), "flags": flags},
                    "temperature": {"tool0": {"actual": 210 if S["printing"] else 25, "target": 210 if S["printing"] else 0},
                                    "bed": {"actual": 60 if S["printing"] else 24, "target": 60 if S["printing"] else 0}}})


def begin(name):
    S.update(file=name, start=time.time(), elapsed_paused=0, paused_at=None,
             printing=True, completion=0.0)


@app.post("/api/files/local")
def upload():
    auth()
    f = request.files["file"]
    f.read()
    S["files"].add(f.filename)
    go = request.form.get("print") == "true" and not S["printing"] and not S["error"]
    if go:
        begin(f.filename)
    return jsonify({"done": True, "effectivePrint": go, "effectiveSelect": True}), 201


@app.delete("/api/files/local/<path:name>")
def delete(name):
    auth()
    S["files"].discard(name)
    return "", 204


@app.post("/api/job")
def command():
    auth(); tick()
    body = request.get_json()
    if not S["printing"]:
        return "", 409
    if body["command"] == "cancel":
        S["printing"] = False
    elif body["command"] == "pause":
        if body.get("action") == "pause" and not S["paused_at"]:
            S["paused_at"] = time.time()
        elif body.get("action") == "resume" and S["paused_at"]:
            S["elapsed_paused"] += time.time() - S["paused_at"]
            S["paused_at"] = None
    return "", 204


@app.post("/fake/external")
def external():
    begin("someone_used_octoprint_directly.gcode")
    return "ok"


@app.post("/fake/error")
def error():
    S["printing"] = False
    S["error"] = "Thermal Runaway"
    return "ok"


@app.post("/fake/reset")
def reset():
    S.update(error=None, printing=False, paused_at=None)
    return "ok"


@app.post("/fake/duration/<float:sec>")
def duration(sec):
    S["duration"] = sec
    return "ok"


MESSAGES = []


@app.post("/fake/webhook")
def webhook():
    """Stands in for a Discord webhook (?wait=true returns the message, like Discord)."""
    msg = dict(request.get_json(), id=str(1000 + len(MESSAGES)))
    MESSAGES.append(msg)
    return (jsonify(msg), 200) if request.args.get("wait") == "true" else ("", 204)


@app.patch("/fake/webhook/messages/<mid>")
def webhook_edit(mid):
    for m in MESSAGES:
        if m["id"] == mid:
            m.update(request.get_json())
            m["edits"] = m.get("edits", 0) + 1
            return jsonify(m)
    return "", 404


@app.get("/fake/messages")
def messages():
    return jsonify(MESSAGES)


@app.post("/fake/blackout/<float:sec>")
def blackout(sec):
    BLACKOUT["until"] = time.time() + sec
    return "ok"


@app.get("/fake/files")
def files():
    return jsonify(sorted(S["files"]))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT)
