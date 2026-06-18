#!/usr/bin/env python3
import json
import mimetypes
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import unquote, urlparse


ASSET_DIR = "/home/pi/screenly_assets"
TOKEN_PATH = "/home/pi/.fleet-monitor-token"
DBUS_PATH = "/tmp/omxplayerdbus.pi"
PORT = 8765


def read_token():
    try:
        with open(TOKEN_PATH, "r") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def dbus_property(name):
    try:
        with open(DBUS_PATH, "r") as handle:
            address = handle.read().strip()
        environment = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=address)
        return subprocess.check_output([
            "dbus-send", "--session", "--print-reply=literal",
            "--dest=org.mpris.MediaPlayer2.omxplayer",
            "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties.Get",
            "string:org.mpris.MediaPlayer2.Player", "string:" + name,
        ], env=environment, stderr=subprocess.STDOUT, timeout=2).decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def player_status():
    position_text = dbus_property("Position")
    metadata_text = dbus_property("Metadata")
    position = re.search(r"int64\s+(\d+)", position_text)
    duration = re.search(r"mpris:length\s+variant\s+int64\s+(\d+)", metadata_text)
    media = re.search(r"file://([^\s]+)", metadata_text)
    media_path = unquote(media.group(1)) if media else ""
    asset_id = os.path.basename(media_path) if media_path.startswith(ASSET_DIR + os.sep) else ""
    return {
        "ok": bool(position and duration and asset_id),
        "assetId": asset_id,
        "position": int(position.group(1)) / 1000000.0 if position else None,
        "duration": int(duration.group(1)) / 1000000.0 if duration else None,
    }


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "FleetMonitor/1.0"

    def authorized(self):
        return bool(self.server.token) and self.headers.get("X-Fleet-Token", "") == self.server.token

    def do_GET(self):
        if not self.authorized():
            self.send_error(403)
            return
        path = urlparse(self.path).path
        if path == "/status":
            self.send_json(player_status())
            return
        if path.startswith("/media/"):
            self.send_media(path[len("/media/"):])
            return
        self.send_error(404)

    def send_json(self, value):
        body = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_media(self, asset_id):
        if not re.match(r"^[A-Za-z0-9_-]{16,80}$", asset_id):
            self.send_error(400)
            return
        filename = os.path.join(ASSET_DIR, asset_id)
        if not os.path.isfile(filename):
            self.send_error(404)
            return
        size = os.path.getsize(filename)
        start, end = 0, size - 1
        range_header = self.headers.get("Range", "")
        match = re.match(r"bytes=(\d*)-(\d*)", range_header)
        if match:
            if match.group(1):
                start = int(match.group(1))
            if match.group(2):
                end = min(int(match.group(2)), size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */{}".format(size))
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(206 if match else 200)
        self.send_header("Content-Type", mimetypes.guess_type("asset.mp4")[0] or "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if match:
            self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        with open(filename, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, _format, *_args):
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.token = read_token()
    server.serve_forever()
