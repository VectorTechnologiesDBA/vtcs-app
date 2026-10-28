#!/usr/bin/env python3
"""
Vector++ community server: the account side of a self-hosted "community cloud" for the Vector++ community-servers
app (github.com/Victor-Rebuild/vector-plus-plus-community-servers).

The app talks to this server over HTTPS:
    GET  /v1                               health check (the app's server prompt probes it)
    POST /v1/create_user                   {"username", "password", "dob"}          -> create an account
    POST /v1/sessions                      {"username", "password"}                 -> {"session": {...}, "user": {...}}
    GET  /v1/session_cert/<name>_<esn>     the robot's session certificate (PEM)

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
import json
import logging
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.error
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

    def valid_token(self, token):
        s = self.sessions.get(token or "")
        return bool(s) and s["expires"] > now_iso()


STORE = Store(CFG["data_dir"])


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


class OtaOnlyHandler(BaseHTTPRequestHandler):
    """Plain-HTTP listener for OS updates only (ota_http_port), for robots that can't do modern HTTPS."""
    server_version = "VectorPlusPlusCommunity/1.0"

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


class Handler(BaseHTTPRequestHandler):
    server_version = "VectorPlusPlusCommunity/1.0"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.client_address[0], fmt % args)

    def send_json(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
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
        if path in ("/v1", ""):
            return self.send_json(200, {"status": "ok", "server": "vector-plus-plus-community"})
        if path.startswith("/v1/session_cert/"):
            return self.session_cert(path[len("/v1/session_cert/"):])
        return self.send_json(404, error("not_found", "Unknown path."))

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.startswith(PROXIED):
            return self.proxy("POST")
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
        if path == "/v1/sessions":
            user = STORE.check_password(username, password)
            if user is None:
                return self.send_json(403, error("invalid_credentials", "Wrong email or password."))
            token, s = STORE.new_session(user)
            log.info("signed in %s", username)
            return self.send_json(200, {
                "session": {"session_token": token, "user_id": user["user_id"], "scope": "user",
                            "time_created": s["created"], "time_expires": s["expires"]},
                "user": public_user(user),
            })
        return self.send_json(404, error("not_found", "Unknown path."))

    def proxy(self, method):
        """Pass a request through to wire-pod's web server, for signed-in users only. Streams the answer (the
        camera feed never ends), so nothing is buffered."""
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not STORE.valid_token(token):
            return self.send_json(401, error("not_signed_in", "Sign in to use this."))
        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > 1 << 20:
                return self.send_json(413, error("too_large", "Request body too large."))
            body = self.rfile.read(length) if length else b""
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

    def ota(self, path, head=False):
        """An OS update file from ota_dir (e.g. /vic/full/latest.ota -> ota_dir/full/latest.ota), with Range support
        so interrupted downloads can resume."""
        rel = path[len("/vic/"):]
        if not OTA_PATH.match(rel):
            return self.send_json(404, error("not_found", "No such update."))
        root = os.path.realpath(CFG["ota_dir"])
        full = os.path.realpath(os.path.join(root, *rel.split("/")))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self.send_json(404, error("not_found", "No such update: " + rel))
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
        url = CFG["wirepod_url"].rstrip("/") + "/session-certs/" + esn
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                cert = r.read()
        except urllib.error.HTTPError as e:
            return self.send_json(404 if e.code == 404 else 502,
                                  error("no_cert", "wire-pod has no session certificate for %s yet (HTTP %d)." % (esn, e.code)))
        except (urllib.error.URLError, OSError) as e:
            return self.send_json(502, error("wirepod_unreachable", "Couldn't reach wire-pod at %s (%s)." % (CFG["wirepod_url"], e)))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(cert)))
        self.end_headers()
        self.wfile.write(cert)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
    log.info("Vector++ community server on https://%s:%s (wire-pod: %s)", CFG["host"], CFG["port"], CFG["wirepod_url"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
