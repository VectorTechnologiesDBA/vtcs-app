#!/usr/bin/env python3
"""
VTCS (Vector Tech Community Server): the account side of a self-hosted "community cloud" for the Vector++ community-servers
app (github.com/Victor-Rebuild/vector-plus-plus-community-servers).

The app talks to this server over HTTPS:
    GET  /v1                               health check (the app's server prompt probes it)
    POST /v1/create_user                   {"username", "password", "dob"}          -> create an account
    POST /v1/sessions                      {"username", "password"}                 -> {"session": {...}, "user": {...}}
    GET  /v1/session_cert/<name>_<esn>     the robot's session certificate (PEM)

The account page (account.html on the website, portal_url; the server's own / redirects there) uses those plus,
signed in, from the site's origins (site_origins, allowed by CORS):
    GET  /v1/me                            the account and its robots
    POST /v1/robots/release                {"esn"}   take a robot off the account (e.g. after a factory reset)
    POST /v1/logout                        end this session

For the app's extra features (Weather and Knowledge settings, Talk, Drive with its live camera, Quick actions,
custom eye colors, SSH console), it also passes these through to wire-pod, for signed-in users only
(Authorization: Bearer <session token from /v1/sessions>):
    /api-sdk/*      wire-pod's SDK API (robot control, settings)
    /api/*          wire-pod's settings API (weather and knowledge graph providers)
    /cam-stream     Vector's camera (MJPEG, streamed)

It also hosts your CFW's OS updates (no sign-in needed: robots download them themselves):
    /vic/full/latest.ota, /vic/full/lkg.ota, /vic/full/<version>.ota, /vic/diff/<version>.ota
served from ota_dir. Optionally also over plain HTTP (ota_http_port) for robots on old recovery firmware.

Everything Vector himself needs (voice, jdocs, token server, weather, knowledge graph, SDK) is done by wire-pod.
Session certificates come from wire-pod too: it captures them when Vector authenticates, and serves them at
http://<wire-pod>:8080/session-certs/<esn>.

Standard library only, so it runs on a Raspberry Pi with nothing to install. Settings: see config.example.json.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import tarfile
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8443,
    "cert_file": "/etc/letsencrypt/live/server.vectortech.dev/fullchain.pem",
    "key_file": "/etc/letsencrypt/live/server.vectortech.dev/privkey.pem",
    "wirepod_url": "http://127.0.0.1:8080",
    "data_dir": os.path.join(HERE, "data"),
    "session_days": 3650,
    "allow_signup": True,
    "ota_dir": os.path.join(HERE, "ota"),
    "ota_http_port": 0,
    "perbot_dir": "/etc/wire-pod/per-bot",
    "relay_dir": "/run/vtcs-relay",
    "relay_ok_dir": "/run/vtcs-relay-ok",
    "portal_url": "https://vectortech.dev/account.html",
    "site_origins": ["https://vectortech.dev", "https://www.vectortech.dev"],
    # VIM (Vector Instant Messaging): plain HTTP on 127.0.0.1 only, for wire-pod on this machine. 0 turns VIM's
    # robot side off.
    "vim_port": 8089,
}

log = logging.getLogger("vpp")


def load_config():
    path = os.environ.get("VPP_CONFIG", os.path.join(HERE, "config.json"))
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


CFG = load_config()


def now_iso(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    """Users and sessions in data/users.json and data/sessions.json. Passwords are scrypt-hashed."""

    def __init__(self, data_dir):
        self.dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.lock = threading.Lock()
        self.users = self._read("users.json")        # email (lower) -> user record
        self.sessions = self._read("sessions.json")  # token -> {user_id, created, expires}
        self.robots = self._read("robots.json")      # esn -> {user_id, claimed}
        self.settings = self._read("settings.json")  # user_id -> {"weather": {...}, "knowledge": {...}}

    def _read(self, name):
        path = os.path.join(self.dir, name)
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _write(self, name, data):
        path = os.path.join(self.dir, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    @staticmethod
    def _hash(password, salt):
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)

    def create_user(self, email, password, dob):
        key = email.strip().lower()
        with self.lock:
            if key in self.users:
                return None
            salt = secrets.token_bytes(16)
            user = {
                "user_id": secrets.token_hex(12),
                "email": email.strip(),
                "dob": dob or "1990-01-01",
                "created": now_iso(),
                "salt": base64.b64encode(salt).decode(),
                "hash": base64.b64encode(self._hash(password, salt)).decode(),
            }
            self.users[key] = user
            self._write("users.json", self.users)
            return user

    def check_password(self, email, password):
        user = self.users.get(email.strip().lower())
        if not user:
            return None
        expected = base64.b64decode(user["hash"])
        actual = self._hash(password, base64.b64decode(user["salt"]))
        return user if hmac.compare_digest(expected, actual) else None

    def new_session(self, user):
        created = datetime.now(timezone.utc)
        expires = created + timedelta(days=int(CFG["session_days"]))
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[token] = {"user_id": user["user_id"], "created": now_iso(created), "expires": now_iso(expires)}
            self._write("sessions.json", self.sessions)
        return token, self.sessions[token]

    def user_id_for(self, token):
        """The signed-in user's id for a session token, or None if the token is unknown or expired."""
        s = self.sessions.get(token or "")
        return s["user_id"] if s and s["expires"] > now_iso() else None

    # Robots belong to the first account that sets them up (claims them, see Handler.session_cert), and only
    # that account can drive them, watch their camera or change their settings. data/robots.json: esn -> record.

    def owner_of(self, esn):
        r = self.robots.get(esn)
        return r["user_id"] if r else None

    def claim(self, esn, user_id):
        """Give an unclaimed robot to user_id. True if user_id owns it afterwards."""
        with self.lock:
            r = self.robots.get(esn)
            if r:
                return r["user_id"] == user_id
            self.robots[esn] = {"user_id": user_id, "claimed": now_iso()}
            self._write("robots.json", self.robots)
        log.info("robot %s claimed by user %s", esn, user_id)
        write_perbot(esn)
        return True

    def release(self, esn):
        with self.lock:
            if self.robots.pop(esn, None) is None:
                return False
            self._write("robots.json", self.robots)
        write_perbot(esn)
        return True

    def end_session(self, token):
        with self.lock:
            if self.sessions.pop(token, None) is not None:
                self._write("sessions.json", self.sessions)

    def set_location(self, esn, location):
        """The robot's weather location, kept here (robots out on the internet can't be reached to set their own)."""
        with self.lock:
            r = self.robots.get(esn)
            if not r:
                return
            if location:
                r["location"] = location
            else:
                r.pop("location", None)
            self._write("robots.json", self.robots)
        write_perbot(esn)

    def robots_of(self, user_id):
        return sorted(esn for esn, r in self.robots.items() if r["user_id"] == user_id)

    # Each account's own weather and knowledge graph settings. data/settings.json: user_id -> {"weather", "knowledge"}.

    def settings_of(self, user_id):
        return self.settings.get(user_id, {})

    def set_settings(self, user_id, section, values):
        with self.lock:
            self.settings.setdefault(user_id, {})[section] = values
            self._write("settings.json", self.settings)
        for esn in self.robots_of(user_id):
            write_perbot(esn)


STORE = Store(CFG["data_dir"])


class Vim:
    """VIM, Vector Instant Messaging, in data/vim.json.

    Every robot has an add code. Entering someone's code in the app makes the two robots contacts of each other,
    each under the name its owner gives the other. A message to a contact (said to Vector, "text Bob", or typed in
    the app) waits in the queue until wire-pod announces it on the other robot (VimInternal: /vim/pending)."""

    ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: nobody should lose a friendship over "is that a zero"
    MAX_TEXT = 300
    MAX_QUEUED = 20  # per recipient. Past 20 unread messages it stops being chatting and starts being a hostage situation

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "vim.json")
        self.lock = threading.Lock()
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        self.robots = d.get("robots") or {}  # esn -> {code, name, contacts: {esn: name}}
        self.queue = d.get("queue") or []    # [{id, to, from, text, sent}]

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"robots": self.robots, "queue": self.queue}, f, indent=2)
        os.replace(tmp, self.path)

    def _robot(self, esn):
        """The robot's record, made (with a new code) the first time. Call with the lock held."""
        r = self.robots.get(esn)
        if r is None:
            codes = {x["code"] for x in self.robots.values()}
            code = ""
            while not code or code in codes:
                code = "".join(secrets.choice(self.ALPHABET) for _ in range(6))
            owner = STORE.owner_of(esn)
            email = next((u["email"] for u in STORE.users.values() if u["user_id"] == owner), "")
            r = self.robots[esn] = {"code": code, "name": email.split("@")[0][:30] or "Vector", "contacts": {}}
            self._save()
        return r

    def info(self, esn):
        with self.lock:
            r = self._robot(esn)
            return {"esn": esn, "code": r["code"], "name": r["name"],
                    "contacts": [{"esn": e, "name": n} for e, n in sorted(r["contacts"].items(), key=lambda x: x[1].lower())]}

    def set_name(self, esn, name):
        with self.lock:
            self._robot(esn)["name"] = name
            self._save()

    def add(self, esn, code, name):
        """Make esn and the robot with that code contacts of each other. (its name, None) or (None, why not)."""
        code = code.strip().upper().replace(" ", "").replace("-", "")
        with self.lock:
            me = self._robot(esn)
            other = next((e for e, r in self.robots.items() if r["code"] == code), None)
            if other is None:
                return None, "No robot has that code. Check it in the other person's app."
            if other == esn:
                # your own code: a contact for yourself, so one robot can try VIM out ("text me" -> he says it).
                # For people whose only friend is their robot. No judgment. He's a great listener.
                me["contacts"][esn] = name or me["name"]
                self._save()
                return me["contacts"][esn], None
            me["contacts"][other] = name or self.robots[other]["name"]
            self.robots[other]["contacts"][esn] = me["name"]
            self._save()
            return me["contacts"][other], None

    def remove(self, esn, other):
        with self.lock:
            self._robot(esn)["contacts"].pop(other, None)
            self._robot(esn).get("sounds", {}).pop(other, None)
            if other in self.robots:
                self.robots[other]["contacts"].pop(esn, None)
            self._save()

    def rename(self, esn, other, name):
        with self.lock:
            contacts = self._robot(esn)["contacts"]
            if other not in contacts:
                return False
            contacts[other] = name
            self._save()
            return True

    def send(self, esn, to, text):
        """Queue a message from esn to its contact to. None, or why it wasn't sent."""
        text = " ".join(str(text).split())[:self.MAX_TEXT]
        if not text:
            return "The message is empty."
        with self.lock:
            if to not in self._robot(esn)["contacts"] or esn not in (self.robots.get(to) or {}).get("contacts", {}):
                return "That robot isn't one of your contacts."
            if sum(1 for m in self.queue if m["to"] == to) >= self.MAX_QUEUED:
                return "Their robot has too many messages waiting. Try again later."
            self.queue.append({"id": secrets.token_hex(8), "to": to, "from": esn, "text": text, "sent": int(time.time())})
            self._save()
        log.info("VIM message %s -> %s", esn, to)
        return None

    def pending(self):
        """Waiting messages, with the sender's name as the recipient knows them."""
        with self.lock:
            return [{"id": m["id"], "to": m["to"], "from": m["from"], "text": m["text"], "sent": m["sent"],
                     "from_name": (self.robots.get(m["to"]) or {}).get("contacts", {}).get(m["from"])
                                  or (self.robots.get(m["from"]) or {}).get("name") or "someone"}
                    for m in self.queue]

    def delivered(self, msg_id):
        with self.lock:
            before = len(self.queue)
            self.queue = [m for m in self.queue if m["id"] != msg_id]
            if len(self.queue) != before:
                self._save()

    def contacts(self, esn):
        """{contact esn: {"name", "sounds"}}: "sounds" are what speech-to-text hears for that name, taught by voice
        ("learn how to say Jacek" -> "jae sick")."""
        with self.lock:
            r = self._robot(esn)
            sounds = r.get("sounds") or {}
            return {e: {"name": n, "sounds": list(sounds.get(e, []))} for e, n in r["contacts"].items()}

    MAX_SOUNDS = 5  # per contact. If it takes more than five tries, the name may be the problem, not Vector

    def learn_sound(self, esn, other, heard):
        """Remember that speech-to-text hears `heard` when esn's owner says that contact's name."""
        heard = " ".join(str(heard).lower().split())[:60]
        with self.lock:
            r = self._robot(esn)
            if not heard or other not in r["contacts"]:
                return False
            lst = r.setdefault("sounds", {}).setdefault(other, [])
            if heard in lst:
                lst.remove(heard)
            lst.insert(0, heard)
            del lst[self.MAX_SOUNDS:]
            self._save()
            return True


VIM = Vim(CFG["data_dir"])


class Quiet:
    """Quiet hours for each robot, in data/quiet.json: esn -> {"rev": n, "settings": {...}}. Set in the app; wire-pod
    sends them to the robot (VictorOS's "vtcs_quiet" app intent) whenever rev changes. VictorOS applies them in its
    sleep cycle: during quiet hours only "Hey Vector" wakes him, and bedtime "sleep" sends him to bed."""

    DEFAULT = {"enabled": False, "bedtime": "nothing", "snore": True,
               "days": [{"on": True, "start": "22:00", "end": "07:00"} for _ in range(7)]}  # Sunday first
    TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "quiet.json")
        self.lock = threading.Lock()
        try:
            with open(self.path, encoding="utf-8") as f:
                self.robots = json.load(f)
        except (OSError, ValueError):
            self.robots = {}

    def get(self, esn):
        r = self.robots.get(esn)
        return json.loads(json.dumps(r["settings"] if r else self.DEFAULT))

    @classmethod
    def clean(cls, b):
        """Settings from the app, checked. (settings, None) or (None, what's wrong)."""
        days = b.get("days")
        if not isinstance(days, list) or len(days) != 7:
            return None, "Send 7 days, Sunday first."
        out = []
        for d in days:
            if not isinstance(d, dict):
                return None, "Bad day."
            start, end = str(d.get("start") or ""), str(d.get("end") or "")
            if not cls.TIME.match(start) or not cls.TIME.match(end):
                return None, "Use 24-hour times like 22:00."
            out.append({"on": bool(d.get("on")), "start": start, "end": end})
        return {"enabled": bool(b.get("enabled")), "bedtime": "sleep" if b.get("bedtime") == "sleep" else "nothing",
                "snore": bool(b.get("snore", True)), "days": out}, None

    def set(self, esn, settings):
        with self.lock:
            rev = (self.robots.get(esn) or {}).get("rev", 0) + 1
            self.robots[esn] = {"rev": rev, "settings": settings}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.robots, f, indent=2)
            os.replace(tmp, self.path)

    def all(self):
        with self.lock:
            return json.loads(json.dumps(self.robots))


QUIET = Quiet(CFG["data_dir"])


def write_perbot(esn):
    """Give wire-pod a robot's settings (its owner's), in perbot_dir/<esn>.json; wire-pod reads them for every
    weather and knowledge graph request (wire-pod-patch/). An unowned robot's file is removed: it uses wire-pod's
    global settings."""
    d = CFG["perbot_dir"]
    if not d:
        return
    path = os.path.join(d, esn + ".json")
    owner = STORE.owner_of(esn)
    settings = dict(STORE.settings_of(owner)) if owner else {}
    location = (STORE.robots.get(esn) or {}).get("location") if owner else None
    if location:
        settings["location"] = location
    try:
        os.makedirs(d, exist_ok=True)
        if not settings:
            if os.path.exists(path):
                os.remove(path)
            return
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f)
        os.replace(tmp, path)
    except OSError as e:
        log.error("couldn't write %s: %s", path, e)


def robot_reachable(esn):
    """Whether wire-pod can reach the robot: through a verified relay link (see Relay), or directly at a private
    (home network) address. Unknown counts as reachable."""
    if os.path.exists(os.path.join(CFG["relay_ok_dir"], esn)):
        return True
    try:
        with urllib.request.urlopen(CFG["wirepod_url"].rstrip("/") + "/api-sdk/get_sdk_info", timeout=5) as r:
            robots = json.loads(r.read().decode("utf-8")).get("robots") or []
    except (urllib.error.URLError, OSError, ValueError):
        return True
    for r in robots:
        if str(r.get("esn", "")).lower() == esn:
            try:
                return ipaddress.ip_address(str(r.get("ip_address", ""))).is_private
            except ValueError:
                return True
    return True


_ota_versions = {}


def version_key(version):
    """Comparable form of a VictorOS version like 3.0.2.42d (numbers compared as numbers)."""
    return [int(x) if x.isdigit() else x for x in re.findall(r"\d+|[a-z]+", version.lower())]


_releases = {}


def release_info(version):
    """When the VictorOS GitHub release for this version came out and its notes (cached for an hour)."""
    cached = _releases.get(version)
    if cached and time.time() - cached[0] < 3600:
        return dict(cached[1])
    info = {}
    try:
        req = urllib.request.Request("https://api.github.com/repos/VectorTechnologiesDBA/VictorOS/releases/tags/v"
                                     + version, headers={"Accept": "application/vnd.github+json",
                                                         "User-Agent": "VTCS"})
        with urllib.request.urlopen(req, timeout=8) as r:
            rel = json.load(r)
        published = rel.get("published_at") or ""
        if published:
            info["released"] = int(datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).timestamp())
        # Spoken by the robot: drop markdown (headings, bullets, bold, links) and bare URLs.
        notes = re.sub(r"\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1), rel.get("body") or "")
        notes = re.sub(r"(?m)^\s*(#+|[-*])\s*", "", notes)
        notes = re.sub(r"https?://\S+", "", notes.replace("**", "").replace("`", "").replace("→", "to"))
        info["notes"] = " ".join(l.strip().rstrip(".") + "." for l in notes.splitlines() if l.strip())
    except (OSError, ValueError):
        pass
    _releases[version] = (time.time(), info)
    return dict(info)


def ota_version(path):
    """The update_version in an OTA file's manifest.ini (cached by modification time), or "" if unreadable."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    cached = _ota_versions.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    version = ""
    try:
        with tarfile.open(path, "r:*") as t:
            member = t.next()
            if member is not None and member.name.endswith("manifest.ini"):
                text = t.extractfile(member).read().decode("utf-8", "replace")
                m = re.search(r"update_version\s*=\s*(\S+)", text)
                version = m.group(1) if m else ""
    except (OSError, tarfile.TarError):
        pass
    _ota_versions[path] = (mtime, version)
    return version


RELAY_PORT = 61443  # above Linux's outgoing-port range (32768-60999), so nothing else ever grabs it


def relay_addr(esn):
    """Where a relayed robot's control API is served: its own loopback address (127.x.y.z, from the ESN) on
    RELAY_PORT. wire-pod reads it from relay_ok_dir/<esn>. One 127.0.0.1 port per robot used to collide with
    outgoing connections, which once held a robot's port hostage indefinitely. Robots deserve their own address."""
    n = int(esn, 16)
    return "127.%d.%d.%d" % (64 + (n >> 16) % 128, (n >> 8) & 255, max(1, n & 255)), RELAY_PORT


class Relay(threading.Thread):
    """Robots on other networks hold an SSH link to this server (vtcs-relay-sshd, port 2222) that ends in a Unix
    socket, relay_dir/<esn>.sock, leading to the robot's control API (vic-gateway). Every robot shares the link key
    from VictorOS, so a link is only used once the robot behind it shows the TLS certificate wire-pod recorded for
    that ESN (only the real robot holds its key). Then it's served on relay_addr(esn), and
    relay_ok_dir/<esn> tells wire-pod to use that address."""

    def __init__(self):
        super().__init__(daemon=True)
        self.active = {}    # esn -> (socket inode, listening socket)
        self.rejected = {}  # esn -> (inode, time) of a link that failed verification

    def run(self):
        os.makedirs(CFG["relay_ok_dir"], exist_ok=True)
        for f in os.listdir(CFG["relay_ok_dir"]):
            self._unmark(f)
        while True:
            try:
                self.scan()
            except Exception as e:  # keep relaying whatever happens
                log.error("relay scan: %s", e)
            time.sleep(10)

    def scan(self):
        seen = set()
        names = os.listdir(CFG["relay_dir"]) if os.path.isdir(CFG["relay_dir"]) else []
        for name in names:
            m = re.match(r"^([0-9a-f]{8})\.sock$", name)
            if not m:
                continue
            esn, path = m.group(1), os.path.join(CFG["relay_dir"], name)
            try:
                ino = os.stat(path).st_ino
            except OSError:
                continue
            seen.add(esn)
            if esn in self.active and self.active[esn][0] == ino:
                continue
            # A link that failed its check is retried every minute (e.g. the robot linked before its gateway was up).
            if self.rejected.get(esn, (None, 0))[0] == ino and time.time() - self.rejected[esn][1] < 60:
                continue
            self.close_link(esn)
            if self.verify(esn, path):
                self.open_link(esn, path, ino)
            else:
                self.rejected[esn] = (ino, time.time())
        for esn in list(self.active):
            if esn not in seen:
                self.close_link(esn)

    def verify(self, esn, path):
        try:
            with urllib.request.urlopen(CFG["wirepod_url"].rstrip("/") + "/session-certs/" + esn, timeout=10) as r:
                pem = r.read().decode("ascii", "replace")
            first = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", pem, re.S)
            expected = ssl.PEM_cert_to_DER_cert(first.group(0))
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            raw.settimeout(15)
            raw.connect(path)
            with ctx.wrap_socket(raw) as t:
                got = t.getpeercert(binary_form=True)
        except (OSError, ValueError, AttributeError, urllib.error.URLError, ssl.SSLError) as e:
            log.warning("relay %s: couldn't verify (%s)", esn, e)
            return False
        if got != expected:
            log.warning("relay %s: the robot's certificate doesn't match wire-pod's; not using this link", esn)
            return False
        return True

    def open_link(self, esn, path, ino):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        host, port = relay_addr(esn)
        try:
            srv.bind((host, port))
            srv.listen(16)
        except OSError as e:
            log.error("relay %s: can't listen on %s:%d (%s)", esn, host, port, e)
            srv.close()
            return
        self.active[esn] = (ino, srv)
        threading.Thread(target=self._accept, args=(srv, path), daemon=True).start()
        with open(os.path.join(CFG["relay_ok_dir"], esn), "w") as f:
            f.write("%s:%d\n" % (host, port))
        log.info("relay %s: robot connected, reachable on %s:%d", esn, host, port)

    def close_link(self, esn):
        entry = self.active.pop(esn, None)
        if entry:
            entry[1].close()
            log.info("relay %s: link closed", esn)
        self._unmark(esn)

    def _unmark(self, esn):
        try:
            os.remove(os.path.join(CFG["relay_ok_dir"], esn))
        except OSError:
            pass

    @staticmethod
    def _accept(srv, path):
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return  # stopped
            try:
                up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                up.connect(path)
            except OSError:
                conn.close()
                continue
            for a, b in ((conn, up), (up, conn)):
                threading.Thread(target=Relay._pump, args=(a, b), daemon=True).start()

    @staticmethod
    def _pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def public_user(user):
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "email_is_verified": True,
        "dob": user["dob"],
        "status": "active",
        "created_by_app_name": "chewie",
        "password_is_complex": True,
    }


def error(code, message):
    return {"code": code, "message": message}


# The app's sign-up form already demands 8-32 characters with upper, lower and a digit; check the same here.
PASSWORD_OK = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,32}$")
ESN = re.compile(r"^[0-9a-fA-F]{8}$")
PROXIED = ("/api-sdk/", "/api/", "/cam-stream")
OTA_PATH = re.compile(r"^(full|diff)/[A-Za-z0-9._-]+\.ota$")
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
              "proxy-authenticate", "authorization", "host", "content-length"}


class QuietLog:
    """Internet scanners hit these ports all day probing for routers and admin pages. Keep their requests (anything
    refused, not found, malformed, or just the front page) out of the log; real traffic is still logged."""

    def log_request(self, code="-", size="-"):
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = 0
        if code >= 400 or (code == 302 and self.path.split("?", 1)[0] in ("/", "/favicon.ico")):
            return
        super().log_request(code, size)

    def log_error(self, fmt, *args):
        pass


class OtaOnlyHandler(QuietLog, BaseHTTPRequestHandler):
    """Plain-HTTP listener for OS updates only (ota_http_port), for robots that can't do modern HTTPS."""
    server_version = "VTCS/1.0"

    def log_message(self, fmt, *args):
        log.info("%s (http) %s", self.client_address[0], fmt % args)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/vic/"):
            return Handler.ota(self, path)
        Handler.send_json(self, 404, error("not_found", "Only /vic/ updates are served over plain HTTP."))

    def do_HEAD(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/vic/"):
            return Handler.ota(self, path, head=True)
        Handler.send_json(self, 404, error("not_found", "Only /vic/ updates are served over plain HTTP."))


class Handler(QuietLog, BaseHTTPRequestHandler):
    server_version = "VTCS/1.0"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.client_address[0], fmt % args)

    def end_headers(self):
        # The website's account page calls this server from another origin.
        origin = self.headers.get("Origin") or ""
        if origin in CFG["site_origins"]:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_json(self, status, body, public=False):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        if public:
            # readable from any web page (vectortech.dev/recovery shows the latest version)
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 65536:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def do_HEAD(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/vic/"):
            return self.ota(path, head=True)
        return self.send_json(404, error("not_found", "Unknown path."))

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.startswith("/vic/"):
            return self.ota(path)
        if path.startswith(PROXIED):
            return self.proxy("GET")
        if path in ("", "/account"):
            return self.portal()
        if path == "/v1/me":
            return self.me()
        if path == "/v1/quiet":
            return self.quiet({"esn": (self.query().get("serial") or [""])[0]})
        if path == "/v1/vim":
            return self.vim("info", {"esn": (self.query().get("serial") or [""])[0]})
        if path == "/v1":
            return self.send_json(200, {"status": "ok", "server": "vtcs"})
        if path.startswith("/v1/session_cert/"):
            return self.session_cert(path[len("/v1/session_cert/"):])
        return self.send_json(404, error("not_found", "Unknown path."))

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.startswith(PROXIED):
            return self.proxy("POST")
        if path == "/v1/logout":
            STORE.end_session(self.token())
            return self.send_json(200, {"status": "signed_out"})
        body = self.read_json()
        if body is None:
            return self.send_json(400, error("bad_request", "Send a JSON body."))
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if path == "/v1/create_user":
            if not CFG["allow_signup"]:
                return self.send_json(403, error("signup_disabled", "This server doesn't accept new accounts."))
            if "@" not in username or len(username) > 254:
                return self.send_json(400, error("invalid_username", "Use an email address as the username."))
            if not PASSWORD_OK.match(password):
                return self.send_json(400, error("invalid_password", "Use 8-32 characters with upper and lower case letters and a digit."))
            user = STORE.create_user(username, password, str(body.get("dob") or ""))
            if user is None:
                return self.send_json(409, error("account_exists", "An account with that email already exists."))
            log.info("created account %s", username)
            return self.send_json(200, {"user": public_user(user)})
        if path == "/v1/quiet":
            return self.quiet(body, save=True)
        if path.startswith("/v1/vim/"):
            return self.vim(path[len("/v1/vim/"):], body)
        if path == "/v1/robots/release":
            user_id = self.user_id()
            esn = str(body.get("esn") or "").strip().lower()
            if not user_id:
                return self.send_json(401, error("not_signed_in", "Sign in to use this."))
            if STORE.owner_of(esn) != user_id:
                return self.send_json(403, error("not_your_robot", "That robot isn't set up on your account."))
            STORE.release(esn)
            log.info("robot %s released by user %s", esn, user_id)
            return self.send_json(200, {"status": "released"})
        if path == "/v1/sessions":
            user = STORE.check_password(username, password)
            if user is None:
                time.sleep(1)  # slows down password guessing
                return self.send_json(403, error("invalid_credentials", "Wrong email or password."))
            token, s = STORE.new_session(user)
            log.info("signed in %s", username)
            return self.send_json(200, {
                "session": {"session_token": token, "user_id": user["user_id"], "scope": "user",
                            "time_created": s["created"], "time_expires": s["expires"]},
                "user": public_user(user),
            })
        return self.send_json(404, error("not_found", "Unknown path."))

    def token(self):
        auth = self.headers.get("Authorization") or ""
        return auth[7:].strip() if auth.lower().startswith("bearer ") else ""

    def user_id(self):
        return STORE.user_id_for(self.token())

    def quiet(self, body, save=False):
        """Quiet hours for a robot on the signed-in account:
            GET  /v1/quiet?serial=<esn>      {"enabled", "bedtime": "sleep"|"nothing", "snore", "days": [7 x {on, start, end}]}
            POST /v1/quiet {esn, ...same}    save (wire-pod sends them to the robot)"""
        user_id = self.user_id()
        if not user_id:
            return self.send_json(401, error("not_signed_in", "Sign in to use this."))
        esn = str(body.get("esn") or "").strip().lower()
        if not ESN.match(esn):
            return self.send_json(400, error("bad_serial", "Say which robot (esn)."))
        if STORE.owner_of(esn) != user_id:
            return self.send_json(403, error("not_your_robot", "That robot isn't set up on your account."))
        if save:
            settings, problem = QUIET.clean(body)
            if problem:
                return self.send_json(400, error("bad_settings", problem))
            QUIET.set(esn, settings)
            log.info("quiet hours for %s: %s", esn, "on" if settings["enabled"] else "off")
        return self.send_json(200, QUIET.get(esn))

    def vim(self, action, body):
        """VIM from the app, for a robot on the signed-in account:
            GET  /v1/vim?serial=<esn>                  {esn, code, name, contacts: [{esn, name}]}
            POST /v1/vim/name    {esn, name}           the name this robot gets when someone adds its code
            POST /v1/vim/add     {esn, code, name}     add the robot with that code, calling it name
            POST /v1/vim/rename  {esn, contact, name}
            POST /v1/vim/remove  {esn, contact}
            POST /v1/vim/send    {esn, to, text}       Vector says it on the contact's robot"""
        user_id = self.user_id()
        if not user_id:
            return self.send_json(401, error("not_signed_in", "Sign in to use this."))
        esn = str(body.get("esn") or "").strip().lower()
        if not ESN.match(esn):
            return self.send_json(400, error("bad_serial", "Say which robot (esn)."))
        if STORE.owner_of(esn) != user_id:
            return self.send_json(403, error("not_your_robot", "That robot isn't set up on your account."))
        name = " ".join(str(body.get("name") or "").split())[:30]
        contact = str(body.get("contact") or body.get("to") or "").strip().lower()
        if action == "name":
            if not name:
                return self.send_json(400, error("bad_name", "Enter a name."))
            VIM.set_name(esn, name)
        elif action == "add":
            added, problem = VIM.add(esn, str(body.get("code") or ""), name)
            if problem:
                return self.send_json(404, error("bad_code", problem))
            log.info("VIM: %s added %s", esn, added)
        elif action == "rename":
            if not name or not VIM.rename(esn, contact, name):
                return self.send_json(400, error("bad_contact", "Unknown contact, or no name."))
        elif action == "remove":
            VIM.remove(esn, contact)
        elif action == "send":
            problem = VIM.send(esn, contact, body.get("text") or "")
            if problem:
                return self.send_json(400, error("not_sent", problem))
            return self.send_json(200, {"status": "sent"})
        elif action != "info":
            return self.send_json(404, error("not_found", "Unknown path."))
        return self.send_json(200, VIM.info(esn))

    def portal(self):
        self.send_response(302)
        self.send_header("Location", CFG["portal_url"])
        self.send_header("Content-Length", "0")
        self.end_headers()

    def me(self):
        """The account page's view: who's signed in, and their robots (with what wire-pod knows of each)."""
        user_id = self.user_id()
        if not user_id:
            return self.send_json(401, error("not_signed_in", "Sign in to use this."))
        email = next((u["email"] for u in STORE.users.values() if u["user_id"] == user_id), "")
        try:
            with urllib.request.urlopen(CFG["wirepod_url"].rstrip("/") + "/api-sdk/get_sdk_info", timeout=10) as r:
                known = {str(b.get("esn", "")).lower(): b for b in json.loads(r.read().decode("utf-8")).get("robots") or []}
        except (urllib.error.URLError, OSError, ValueError):
            known = {}
        robots = [{"esn": esn, "claimed": STORE.robots[esn]["claimed"], "ip": known.get(esn, {}).get("ip_address", ""),
                   "seen": esn in known} for esn in STORE.robots_of(user_id)]
        return self.send_json(200, {"email": email, "robots": robots})

    def query(self):
        return urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")

    def proxy(self, method):
        """The extra features, for signed-in users, on their own robots only:
            /api-sdk/get_sdk_info          wire-pod's list, cut down to the user's robots
            /api-sdk/<x>?serial=, /cam-stream?serial=   passed through to wire-pod if the user owns that robot
            /api/{get,set}_{weather,kg}_api              the user's own settings, kept here (see write_perbot)
        Everything else under /api/ is wire-pod's admin API, and isn't reachable from outside."""
        user_id = self.user_id()
        if not user_id:
            return self.send_json(401, error("not_signed_in", "Sign in to use this."))
        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > 1 << 20:
                return self.send_json(413, error("too_large", "Request body too large."))
            body = self.rfile.read(length) if length else b""
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.startswith("/api/"):
            return self.own_settings(user_id, path[len("/api/"):], body)
        if path == "/api-sdk/get_sdk_info":
            return self.sdk_info(user_id)
        serial = (self.query().get("serial") or [""])[0].strip().lower()
        if not ESN.match(serial):
            return self.send_json(400, error("bad_serial", "Say which robot (serial=<ESN>)."))
        if STORE.owner_of(serial) != user_id:
            return self.send_json(403, error("not_your_robot", "That robot isn't set up on your account."))
        # Weather location and settings: answered here. Going through wire-pod would make it connect to the robot
        # itself, which fails (and hangs the app) for robots on other networks.
        if path == "/api-sdk/location":
            STORE.set_location(serial, (self.query().get("location") or [""])[0].strip()[:100])
            return self.send_json(200, {"status": "saved"})
        if path == "/api-sdk/get_sdk_settings":
            return self.sdk_settings(serial)
        # Everything else (Drive, camera, Talk, eye colors, quick actions) makes wire-pod connect to the robot
        # itself, which only works when the robot is on the server's own network.
        if not robot_reachable(serial):
            return self.send_json(409, error("robot_not_reachable",
                                             "Can't reach your Vector right now. Make sure he's on and running VictorOS 3.0.2.41d or newer."))
        return self.pass_through(method, body)

    def sdk_settings(self, serial):
        """wire-pod's settings for the robot if it answers quickly, with the location kept here; just the location
        if the robot can't be reached."""
        info = {}
        try:
            url = CFG["wirepod_url"].rstrip("/") + "/api-sdk/get_sdk_settings?serial=" + serial
            with urllib.request.urlopen(url, timeout=6) as r:
                info = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            pass
        location = (STORE.robots.get(serial) or {}).get("location")
        if location or "default_location" not in info:
            info["default_location"] = location or ""
        return self.send_json(200, info)

    def sdk_info(self, user_id):
        try:
            with urllib.request.urlopen(CFG["wirepod_url"].rstrip("/") + "/api-sdk/get_sdk_info", timeout=15) as r:
                info = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError:
            info = {"robots": []}  # wire-pod answers 500 while no robot has authenticated
        except (urllib.error.URLError, OSError, ValueError) as e:
            return self.send_json(502, error("wirepod_unreachable", "Couldn't reach wire-pod at %s (%s)." % (CFG["wirepod_url"], e)))
        mine = set(STORE.robots_of(user_id))
        info["robots"] = [r for r in info.get("robots") or [] if str(r.get("esn", "")).lower() in mine]
        info.pop("global_guid", None)
        return self.send_json(200, info)

    def own_settings(self, user_id, name, body):
        if name == "get_weather_api":
            w = STORE.settings_of(user_id).get("weather") or {}
            return self.send_json(200, {"enable": bool(w.get("enable")), "provider": w.get("provider", ""),
                                        "key": w.get("key", ""), "unit": w.get("unit", "")})
        if name == "get_kg_api":
            return self.send_json(200, STORE.settings_of(user_id).get("knowledge") or {"enable": False, "provider": ""})
        if name not in ("set_weather_api", "set_kg_api"):
            return self.send_json(403, error("admin_only", "That's a server setting. Change it in wire-pod's web interface."))
        try:
            b = json.loads((body or b"").decode("utf-8"))
            assert isinstance(b, dict)
        except (ValueError, UnicodeDecodeError, AssertionError):
            return self.send_json(400, error("bad_request", "Send a JSON body."))
        if name == "set_weather_api":
            provider = str(b.get("provider") or "").strip()
            if provider not in ("", "openweathermap.org", "weatherapi.com"):
                return self.send_json(400, error("bad_provider", "Unknown weather provider."))
            values = {"enable": provider != "", "provider": provider, "key": str(b.get("key") or "").strip()}
            if b.get("unit") in ("F", "C"):
                values["unit"] = b["unit"]
            STORE.set_settings(user_id, "weather", values)
        else:
            keep = {"enable": bool, "provider": str, "key": str, "id": str, "model": str, "intentgraph": bool, "robotName": str,
                    "openai_prompt": str, "openai_voice": str, "openai_voice_with_english": bool, "save_chat": bool,
                    "commands_enable": bool, "endpoint": str, "top_p": float, "temp": float}
            values = {}
            for k, t in keep.items():
                if k in b and b[k] is not None:
                    try:
                        values[k] = t(b[k]).strip() if t is str else t(b[k])
                    except (TypeError, ValueError):
                        return self.send_json(400, error("bad_value", "Bad value for " + k + "."))
            STORE.set_settings(user_id, "knowledge", values)
        return self.send_json(200, {"status": "saved"})

    def pass_through(self, method, body):
        """Pass the request to wire-pod's web server. Streams the answer (the camera feed never ends), so nothing
        is buffered."""
        req = urllib.request.Request(CFG["wirepod_url"].rstrip("/") + self.path, data=body, method=method)
        for k, v in self.headers.items():
            if k.lower() not in HOP_BY_HOP:
                req.add_header(k, v)
        try:
            upstream = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            upstream = e
        except (urllib.error.URLError, OSError) as e:
            return self.send_json(502, error("wirepod_unreachable", "Couldn't reach wire-pod at %s (%s)." % (CFG["wirepod_url"], e)))
        with upstream:
            self.send_response(upstream.status if hasattr(upstream, "status") else upstream.code)
            for k, v in upstream.headers.items():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            # No Content-Length: the body is streamed until wire-pod closes it.
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    chunk = upstream.read1(65536) if hasattr(upstream, "read1") else upstream.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
                pass  # the app went away (e.g. left the Drive screen)

    def latest_release(self):
        """The newest VictorOS on this server, for the robot's "check for updates" voice command: its version, when it
        was released and its release notes (ota_dir/full/latest-notes.txt, else the GitHub release's notes)."""
        latest = os.path.join(CFG["ota_dir"], "full", "latest.ota")
        version = ota_version(latest)
        if not version:
            return self.send_json(404, error("no_update", "No update on this server."))
        info = release_info(version)
        notes_file = os.path.join(CFG["ota_dir"], "full", "latest-notes.txt")
        try:
            with open(notes_file, encoding="utf-8") as f:
                info["notes"] = f.read().strip()
        except OSError:
            pass
        released = info.get("released") or int(os.path.getmtime(latest))
        self.send_json(200, {"version": version, "released": released, "notes": info.get("notes", "")}, public=True)

    def ota(self, path, head=False):
        """An OS update file from ota_dir (e.g. /vic/full/latest.ota -> ota_dir/full/latest.ota), with Range support
        so interrupted downloads can resume.

        A robot checking for updates asks for {full,diff}/<its own version>.ota: if there's no such file it gets
        full/latest.ota when that's newer, or 403 (what the robot reads as "no update") when it's
        already on it."""
        rel = path[len("/vic/"):]
        if rel == "latest.json":
            return self.latest_release()
        if not OTA_PATH.match(rel):
            return self.send_json(404, error("not_found", "No such update."))
        root = os.path.realpath(CFG["ota_dir"])
        full = os.path.realpath(os.path.join(root, *rel.split("/")))
        if not full.startswith(root + os.sep):
            return self.send_json(404, error("not_found", "No such update: " + rel))
        if not os.path.isfile(full):
            latest = os.path.join(root, "full", "latest.ota")
            asked = rel.split("/", 1)[1][:-len(".ota")]
            newest = ota_version(latest)
            if not newest or version_key(asked) >= version_key(newest):
                return self.send_json(403, error("no_update", "No update available."))
            full = latest
        size = os.path.getsize(full)
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(size - int(m.group(2)), 0)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        if head:
            return
        try:
            with open(full, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            pass

    def session_cert(self, name):
        # "<robot-name>_<esn>", or just "<esn>" when the app doesn't know the name.
        esn = name.rsplit("_", 1)[-1].lower()
        if not ESN.match(esn):
            return self.send_json(400, error("bad_esn", "Expected <name>_<8-digit ESN>."))
        # The app fetches this while it's connected to the robot over Bluetooth during setup, so it's where a
        # robot gets claimed: by the signed-in account, if no one has it yet. Someone else's robot: no cert.
        user_id = self.user_id()
        owner = STORE.owner_of(esn)
        if owner and owner != user_id:
            return self.send_json(403, error("not_your_robot", "This robot is set up on another account."))
        url = CFG["wirepod_url"].rstrip("/") + "/session-certs/" + esn
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                cert = r.read()
        except urllib.error.HTTPError as e:
            return self.send_json(404 if e.code == 404 else 502,
                                  error("no_cert", "wire-pod has no session certificate for %s yet (HTTP %d)." % (esn, e.code)))
        except (urllib.error.URLError, OSError) as e:
            return self.send_json(502, error("wirepod_unreachable", "Couldn't reach wire-pod at %s (%s)." % (CFG["wirepod_url"], e)))
        if user_id and not owner:
            STORE.claim(esn, user_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(cert)))
        self.end_headers()
        self.wfile.write(cert)


class VimInternal(QuietLog, BaseHTTPRequestHandler):
    """VIM for wire-pod on this machine (listens on 127.0.0.1 only, no sign-in):
        GET  /vim/contacts?esn=<esn>       {"contacts": [{esn, name, sounds}]}   who that robot can text
        POST /vim/sound {esn, contact, heard}   learn how speech-to-text hears a contact's name
        POST /vim/send {from, to, text}    a message said to Vector
        GET  /vim/pending                  {"messages": [{id, to, from_name, text, sent}]}   to announce
        GET  /quiet/all                    {"robots": {esn: {rev, settings}}}   quiet hours to send to robots
        POST /vim/delivered {id}           announced: forget it"""

    def log_message(self, fmt, *args):
        pass  # wire-pod polls /vim/pending every few seconds

    def send_json(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        if path == "/vim/contacts":
            esn = (q.get("esn") or [""])[0].lower()
            if not ESN.match(esn):
                return self.send_json(400, error("bad_serial", "Say which robot (esn=)."))
            return self.send_json(200, {"contacts": [{"esn": e, "name": c["name"], "sounds": c["sounds"]}
                                                     for e, c in VIM.contacts(esn).items()]})
        if path == "/vim/pending":
            return self.send_json(200, {"messages": VIM.pending()})
        if path == "/quiet/all":
            return self.send_json(200, {"robots": QUIET.all()})
        return self.send_json(404, error("not_found", "Unknown path."))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if 0 < length <= 65536 else {}
            assert isinstance(body, dict)
        except (ValueError, UnicodeDecodeError, AssertionError):
            return self.send_json(400, error("bad_request", "Send a JSON body."))
        path = self.path.split("?", 1)[0]
        if path == "/vim/send":
            problem = VIM.send(str(body.get("from") or "").lower(), str(body.get("to") or "").lower(), body.get("text") or "")
            if problem:
                return self.send_json(400, error("not_sent", problem))
            return self.send_json(200, {"status": "sent"})
        if path == "/vim/sound":
            ok = VIM.learn_sound(str(body.get("esn") or "").lower(), str(body.get("contact") or "").lower(),
                                 body.get("heard") or "")
            return self.send_json(200 if ok else 400, {"status": "ok"} if ok else error("not_saved", "Unknown contact."))
        if path == "/vim/delivered":
            VIM.delivered(str(body.get("id") or ""))
            return self.send_json(200, {"status": "ok"})
        return self.send_json(404, error("not_found", "Unknown path."))


def admin(args):
    """python3 vpp_server.py robots | release <esn>   (stop the server first: it keeps its own copy of the data)"""
    emails = {u["user_id"]: u["email"] for u in STORE.users.values()}
    if args[0] == "robots":
        for esn, r in sorted(STORE.robots.items()):
            print("%s  %s  (since %s)" % (esn, emails.get(r["user_id"], r["user_id"]), r["claimed"]))
    elif args[0] == "release" and len(args) == 2:
        print("released" if STORE.release(args[1].lower()) else "no one owns " + args[1])
    else:
        sys.exit(admin.__doc__)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) > 1:
        return admin(sys.argv[1:])
    server = ThreadingHTTPServer((CFG["host"], int(CFG["port"])), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(CFG["cert_file"], CFG["key_file"])
    except (OSError, ssl.SSLError) as e:
        sys.exit("Can't load the certificate (%s / %s): %s" % (CFG["cert_file"], CFG["key_file"], e))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    os.makedirs(os.path.join(CFG["ota_dir"], "full"), exist_ok=True)
    if int(CFG.get("ota_http_port") or 0):
        plain = ThreadingHTTPServer((CFG["host"], int(CFG["ota_http_port"])), OtaOnlyHandler)
        threading.Thread(target=plain.serve_forever, daemon=True).start()
        log.info("OS updates also over plain HTTP on port %s", CFG["ota_http_port"])
    if CFG.get("relay_dir"):
        Relay().start()
    if int(CFG.get("vim_port") or 0):
        vim = ThreadingHTTPServer(("127.0.0.1", int(CFG["vim_port"])), VimInternal)
        threading.Thread(target=vim.serve_forever, daemon=True).start()
    log.info("VTCS (Vector Tech Community Server) on https://%s:%s (wire-pod: %s)", CFG["host"], CFG["port"], CFG["wirepod_url"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
