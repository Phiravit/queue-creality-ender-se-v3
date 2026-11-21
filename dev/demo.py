"""Try the queue on your own computer with two fake printers (nothing real is touched).

    python dev/demo.py          then open http://127.0.0.1:8770

Staff password: demo.  Research printer password: research.
Fake prints take 3 minutes. With DISCORD_WEBHOOK_URL set in the terminal, alerts and
the status board go to that Discord channel. Ctrl+C stops everything; the demo data is thrown away.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "app.py")
PORT = int(os.environ.get("DEMO_PORT", "8770"))      # fake printers use the next two ports
F1, F2 = "http://127.0.0.1:%d" % (PORT + 1), "http://127.0.0.1:%d" % (PORT + 2)
Q = "http://127.0.0.1:%d" % PORT

for port in (PORT, PORT + 1, PORT + 2):
    # Windows lets two programs listen on one port, so an old demo would silently mix in.
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            sys.exit("Port %d is still in use: an old demo (or something else) is running.\n"
                     "Close its terminal or stop python.exe in Task Manager, then try again." % port)

data = tempfile.mkdtemp(prefix="pq_demo_")
env = dict(os.environ, QUEUE_CONFIG=os.path.join(data, "none.env"), DATA_DIR=data, PORT=str(PORT),
           HOST="127.0.0.1", ADMIN_PASSWORD="demo", START_GRACE_SECONDS="5", POLL_SECONDS="2",
           PRINTER1_NAME="Free Use Printer (Printer 1)", PRINTER1_URL=F1, PRINTER1_APIKEY="K1",
           PRINTER2_NAME="Research Printer (Printer 2)", PRINTER2_URL=F2, PRINTER2_APIKEY="K2",
           PRINTER2_PASSWORD="research", PUBLIC_URL=Q)
# To see real Discord alerts and the status board, set DISCORD_WEBHOOK_URL in this
# terminal before starting the demo (PowerShell: $env:DISCORD_WEBHOOK_URL = "...").
procs = [subprocess.Popen([sys.executable, os.path.join(HERE, "fake_octoprint.py"), port, key],
                          stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
         for port, key in ((str(PORT + 1), "K1"), (str(PORT + 2), "K2"))]
time.sleep(1.5)
for f in (F1, F2):
    requests.post(f + "/fake/duration/180.0")
procs.append(subprocess.Popen([sys.executable, APP], env=env))


def gcode(name, minutes):
    path = os.path.join(data, name)
    with open(path, "w") as fh:
        fh.write(";FLAVOR:Marlin\n;TIME:%d\nG28\n" % (minutes * 60))
    return path


def as_student(sid, name):
    s = requests.Session()
    s.post(Q + "/login", data={"sid": sid, "name": name})
    return s


def add(s, printer, path, pw=""):
    with open(path, "rb") as fh:
        s.post(Q + "/submit", data={"printer": printer, "printer_password": pw},
               files={"file": (os.path.basename(path), fh)})


try:
    for _ in range(50):
        try:
            if requests.get(Q + "/login", timeout=1).ok:
                break
        except requests.RequestException:
            time.sleep(0.3)
    # A little sample data: one print running on printer 1 with two people waiting.
    a, b, c = (as_student("65130500101", "Somchai Jaidee"), as_student("65130500102", "Nicha Wong"),
               as_student("65130500103", "Arthit Saengdao"))
    add(a, "1", gcode("phone_stand_v2.gcode", 95))
    add(b, "1", gcode("gear_test.gcode", 40))
    add(c, "1", gcode("bracket.gcode", 25))
    a.post(Q + "/printer/1/clear", data={"confirm": "yes"})
    print("\nDemo running: open %s  (staff password: demo, Research printer password: research)" % Q)
    print("Discord: %s" % ("alerts + status board ON" if any(k.startswith("DISCORD") and k.endswith("WEBHOOK_URL") and v for k, v in os.environ.items())
                           else "off (set DISCORD_WEBHOOK_URL to try it)"))
    print("Ctrl+C to stop.\n")
    procs[-1].wait()
except KeyboardInterrupt:
    pass
finally:
    for p in procs:
        p.terminate()
