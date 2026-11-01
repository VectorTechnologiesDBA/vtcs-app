#!/usr/bin/env python3
"""
WireOS build watcher: a small window that follows ~/build.log of a running WireOS (Yocto) build in WSL and shows
progress, an estimate of the time left, what it's working on, and whether it finished or failed.

Run on Windows:  python build_watch.py
Options:         python build_watch.py --log "\\\\wsl$\\Ubuntu-24.04\\home\\hayes\\build.log"

Only reads the log; it never touches the build. Standard library only (Tkinter).

About the estimate: Yocto's task count is uneven. Most tasks are cache restores that take a second, and a few
(compiling victor, building the root filesystem, packing the OTA) take many minutes. The estimate uses the speed
over the last few minutes, then adds a reserve for the slow final packaging steps that always come last, so it
starts optimistic and settles down as the build goes on. Tuned for an 8-core/16-thread laptop CPU (ASUS TUF A16,
Ryzen 7) with 16 GB RAM, where packing the image and OTA takes roughly 8-12 minutes.
"""

import argparse
import collections
import os
import re
import time
import tkinter as tk
from tkinter import ttk

DEFAULT_LOG = r"\\wsl$\Ubuntu-24.04\home\hayes\build.log"
DEFAULT_OTA_DIR = r"\\wsl$\Ubuntu-24.04\home\hayes\wire-os\_build"

PROGRESS = re.compile(r"Running runqueue tasks: *(\d+)% \((\d+)/(\d+)\)")
# The same, when the build runs in a terminal (e.g. under `script`): bitbake redraws a task list instead.
PROGRESS_TTY = re.compile(r"Currently +\d+ running tasks \((\d+) of (\d+)\)")
TASK_TTY = re.compile(r"^\s*\d+: (\S+?)-\d\S* (do_\w+) - ")
TASK = re.compile(r"recipe ([^ ]+): task (do_\w+): (Started|Succeeded|Failed)")
PROBLEM = re.compile(r"buildProblem description='([^']*)'")
DONE = re.compile(r'Script done on .*COMMAND_EXIT_CODE="(\d+)"')

# Final packaging steps (rootfs, image, OTA) that come after the last runqueue progress and take a while.
PACKAGING_RESERVE_S = 10 * 60
# Tasks that are known to be slow; shown with a note so a long pause there isn't mistaken for a hang.
SLOW = ("victor", "linux-msm", "rootfs", "image", "fluoride", "update-engine", "vic-")
RATE_WINDOW_S = 5 * 60
REFRESH_MS = 2000


def fmt(seconds):
    if seconds is None:
        return "--"
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {s:02d}s" if m else f"{s}s")


class Watcher:
    def __init__(self, log_path, ota_dir):
        self.log_path = log_path
        self.ota_dir = ota_dir
        self.offset = 0
        self.partial = ""
        self.done = None, None  # (task, total)
        self.samples = collections.deque()  # (time, tasks done)
        self.running = collections.OrderedDict()  # "recipe do_task" -> start time
        self.problems = []
        self.finished = None  # exit code
        self.started_at = None
        self.last_change = None
        self.last_size = -1

    def poll(self):
        now = time.time()
        try:
            st = os.stat(self.log_path)
        except OSError:
            return "Waiting for the build log…"
        if self.started_at is None:
            self.started_at = getattr(st, "st_birthtime", None) or st.st_ctime
            if now - self.started_at > 6 * 3600:  # old log from a previous build: time from now instead
                self.started_at = now
        if st.st_size < self.offset:  # log was replaced (a new build started)
            self.__init__(self.log_path, self.ota_dir)
            return "New build started"
        if st.st_size != self.last_size:
            self.last_size = st.st_size
            self.last_change = now
        with open(self.log_path, "rb") as f:
            f.seek(self.offset)
            data = f.read()
            self.offset = f.tell()
        text = self.partial + data.decode("utf-8", "replace")
        lines = re.split(r"[\r\n]", text)
        self.partial = lines.pop()  # keep an unfinished last line for next time
        first_read = self.offset == len(data)
        for line in lines:
            self.parse(line, now)
        if first_read and self.samples:
            # The log so far was read in one go, so every progress line got the same timestamp. Keep only the
            # latest as the starting point; the speed is measured from new progress lines.
            self.samples = collections.deque([self.samples[-1]])
        return None

    def parse(self, line, now):
        m = PROGRESS.search(line)
        if m:
            done, total = int(m.group(2)), int(m.group(3))
            self.done = done, total
            self.samples.append((now, done))
        m = PROGRESS_TTY.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            if self.done != (done, total):
                self.done = done, total
                self.samples.append((now, done))
            # A fresh task list follows: keep the start times of tasks that are still in it.
            self.tty_prev, self.running = self.running, collections.OrderedDict()
        m = TASK_TTY.search(line)
        if m:
            key = f"{m.group(1)} {m.group(2)}"
            self.running[key] = getattr(self, "tty_prev", {}).get(key, now)
        for rec, task, state in TASK.findall(line):
            key = f"{rec} {task}"
            if state == "Started":
                self.running.setdefault(key, now)
            else:
                # Some tasks (e.g. do_package_qa) log Started but no Succeeded line: when any task of a recipe
                # finishes, clear that recipe's earlier entries too, so they don't look stuck.
                for k in [k for k in self.running if k == key or (k.split(" ")[0] == rec and self.running[k] <= self.running.get(key, now))]:
                    self.running.pop(k, None)
        # Anything still listed but quietly superseded for over 15 minutes is almost certainly done.
        for k in [k for k, t in self.running.items() if now - t > 900 and not any(s in k for s in SLOW)]:
            self.running.pop(k, None)
        m = PROBLEM.search(line)
        if m and m.group(1) not in self.problems:
            self.problems.append(m.group(1))
        m = DONE.search(line)
        if m:
            self.finished = int(m.group(1))

    def rate(self, now):
        while self.samples and now - self.samples[0][0] > RATE_WINDOW_S and len(self.samples) > 2:
            self.samples.popleft()
        if len(self.samples) < 2:
            return None
        (t0, d0), (t1, d1) = self.samples[0], self.samples[-1]
        if t1 - t0 < 20 or d1 <= d0:
            return None
        return (d1 - d0) / (t1 - t0)  # tasks per second

    def eta(self, now):
        done, total = self.done
        if done is None:
            return None
        r = self.rate(now)
        left = total - done
        if r is None:
            return None if left > 0 else PACKAGING_RESERVE_S
        # Later tasks are heavier than the cache-restore tasks that dominate early speed: slow the rate down
        # the further the build is from done, then add the packaging reserve.
        frac = done / total if total else 0
        drag = 1.0 + 1.5 * (1.0 - frac)
        return left / r * drag + (PACKAGING_RESERVE_S if frac < 0.995 else PACKAGING_RESERVE_S / 2)

    def newest_ota(self):
        try:
            files = [os.path.join(self.ota_dir, n) for n in os.listdir(self.ota_dir) if n.endswith(".ota")]
        except OSError:
            return None
        return max(files, key=os.path.getmtime) if files else None


class App:
    def __init__(self, root, watcher):
        self.w = watcher
        self.root = root
        root.title("WireOS build watcher")
        root.geometry("560x380")
        root.minsize(480, 340)
        pad = {"padx": 14, "pady": 4}

        self.status = tk.Label(root, text="Starting…", font=("Segoe UI", 15, "bold"), anchor="w")
        self.status.pack(fill="x", **pad)
        self.bar = ttk.Progressbar(root, maximum=100)
        self.bar.pack(fill="x", padx=14, pady=(2, 8))

        grid = tk.Frame(root)
        grid.pack(fill="x", padx=14)
        self.vals = {}
        for i, label in enumerate(("Tasks", "Time left (estimate)", "Elapsed", "Speed", "Log last changed")):
            tk.Label(grid, text=label, font=("Segoe UI", 10), fg="#666", anchor="w").grid(row=i, column=0, sticky="w", pady=1)
            v = tk.Label(grid, text="--", font=("Segoe UI", 11, "bold"), anchor="w")
            v.grid(row=i, column=1, sticky="w", padx=12, pady=1)
            self.vals[label] = v

        tk.Label(root, text="Working on", font=("Segoe UI", 10), fg="#666", anchor="w").pack(fill="x", padx=14, pady=(10, 0))
        self.current = tk.Label(root, text="--", font=("Consolas", 10), anchor="nw", justify="left")
        self.current.pack(fill="both", expand=True, padx=14)
        self.note = tk.Label(root, text="", font=("Segoe UI", 9), fg="#666", anchor="w", wraplength=520, justify="left")
        self.note.pack(fill="x", padx=14, pady=(0, 10))
        self.tick()

    def tick(self):
        w = self.w
        msg = w.poll()
        now = time.time()
        done, total = w.done

        if w.finished is not None:
            ota = w.newest_ota()
            if w.finished == 0:
                self.status.config(text="Finished", fg="#1a7f37")
                self.note.config(text=("New update: " + os.path.basename(ota)) if ota else "")
                self.bar["value"] = 100
            else:
                self.status.config(text="Failed", fg="#c62828")
                self.note.config(text="Failed in: " + ", ".join(w.problems[-3:]) if w.problems else "Check ~/build.log")
        elif w.problems:
            self.status.config(text="Error, build stopping…", fg="#c62828")
            self.note.config(text="Failed in: " + ", ".join(w.problems[-3:]))
        elif msg:
            self.status.config(text=msg, fg="#444")
        else:
            self.status.config(text="Building", fg="#1565c0")

        if done is not None:
            pct = 100.0 * done / total if total else 0
            if w.finished is None:
                self.bar["value"] = pct
            self.vals["Tasks"].config(text=f"{done} / {total}  ({pct:.0f}%)")
        r = w.rate(now)
        self.vals["Speed"].config(text=f"{r * 60:.0f} tasks/min" if r else "--")
        eta = None if w.finished is not None else w.eta(now)
        self.vals["Time left (estimate)"].config(text="done" if w.finished is not None else (fmt(eta) if eta else "measuring…"))
        if w.started_at:
            self.vals["Elapsed"].config(text=fmt(now - w.started_at))
        if w.last_change:
            age = now - w.last_change
            self.vals["Log last changed"].config(text=f"{fmt(age)} ago", fg="#c62828" if age > 600 else "black")

        running = list(w.running.items())[-4:]
        if running:
            self.current.config(text="\n".join(f"{k}   ({fmt(now - t)})" for k, t in running))
            slow = [k for k, _ in running if any(s in k for s in SLOW)]
            if w.finished is None and not w.problems:
                self.note.config(text="Slow step in progress (" + slow[0].split()[0] + "): long pauses here are normal." if slow else "")
        elif w.finished is None:
            self.current.config(text="--")
        self.root.after(REFRESH_MS, self.tick)


def main():
    ap = argparse.ArgumentParser(description="Watch a WireOS build log.")
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--ota-dir", default=DEFAULT_OTA_DIR)
    args = ap.parse_args()
    root = tk.Tk()
    App(root, Watcher(args.log, args.ota_dir))
    root.mainloop()


if __name__ == "__main__":
    main()
