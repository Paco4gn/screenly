import os
import sys
import webbrowser
import base64
import ipaddress
import json
import re
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Timer
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = BASE_DIR / "vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

from flask import Flask, Response, jsonify, request, send_file, send_from_directory, stream_with_context
import requests

CONFIG_PATH = BASE_DIR / "fleet.json"
MONITOR_CONFIG_PATH = BASE_DIR / "monitor.json"
CONFIG_LOCK = Lock()
CURRENT_ASSET_LOCK = Lock()
CURRENT_ASSET_CACHE = {}
DEFAULT_FLEET = [
    {"host": f"192.168.20.{number}", "name": f"Pantalla {index}"}
    for index, number in enumerate(range(223, 229), start=1)
]
app = Flask(__name__, static_folder=None)


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/<path:filename>")
def static_file(filename):
    if filename not in {"app.js", "styles.css"}:
        return jsonify(error="Archivo no permitido"), 404
    return send_from_directory(BASE_DIR, filename)


@app.get("/api/hosts")
def get_hosts():
    return jsonify(hosts=load_fleet())


@app.post("/api/hosts")
def add_host():
    data = request.get_json(silent=True) or {}
    host = validate_private_host(data.get("host"))
    name = clean_host_name(data.get("name"), host)
    with CONFIG_LOCK:
        fleet = load_fleet()
        if any(item["host"] == host for item in fleet):
            return jsonify(error="Esa Raspberry ya esta en la flota"), 409
        fleet.append({"host": host, "name": name})
        save_fleet(fleet)
    return jsonify(host={"host": host, "name": name}), 201


@app.patch("/api/hosts/<host>")
def rename_host(host):
    host = validate_private_host(host)
    data = request.get_json(silent=True) or {}
    name = clean_host_name(data.get("name"), host)
    with CONFIG_LOCK:
        fleet = load_fleet()
        item = next((item for item in fleet if item["host"] == host), None)
        if item is None:
            return jsonify(error="La Raspberry no existe en la flota"), 404
        item["name"] = name
        save_fleet(fleet)
    return jsonify(host=item)


@app.delete("/api/hosts/<host>")
def remove_host(host):
    host = validate_private_host(host)
    with CONFIG_LOCK:
        fleet = load_fleet()
        updated = [item for item in fleet if item["host"] != host]
        if len(updated) == len(fleet):
            return jsonify(error="La Raspberry no existe en la flota"), 404
        save_fleet(updated)
    return jsonify(ok=True, host=host)


@app.post("/api/list")
def list_fleet():
    data = request.get_json(silent=True) or {}
    hosts = clean_hosts(data.get("hosts"))
    auth = auth_from(data)
    preferred = data.get("apiVersion", "auto")
    with ThreadPoolExecutor(max_workers=len(hosts) or 1) as executor:
        results = list(executor.map(lambda host: list_host(host, auth, preferred), hosts))
    return jsonify(results=results)


@app.post("/api/now")
def now_playing_fleet():
    data = request.get_json(silent=True) or {}
    hosts = clean_hosts(data.get("hosts"))
    auth = auth_from(data)
    with ThreadPoolExecutor(max_workers=len(hosts) or 1) as executor:
        results = list(executor.map(lambda host: current_asset(host, auth), hosts))
    return jsonify(results=results, checkedAt=datetime.now(timezone.utc).isoformat())


def current_asset(host, auth):
    telemetry = monitor_status(host)
    asset = None
    if telemetry.get("ok") and telemetry.get("assetId"):
        with CURRENT_ASSET_LOCK:
            cached = CURRENT_ASSET_CACHE.get(host)
            if cached and str(cached.get("asset_id")) == str(telemetry["assetId"]):
                asset = dict(cached)
        if asset is None:
            response = screenly_request("GET", host, "/api/v1.2/assets", auth, timeout=8)
            assets = normalize_assets(response.get("data")) if response["ok"] else None
            asset = find_asset(assets or [], telemetry["assetId"])
    else:
        response = screenly_request("GET", host, "/api/v1/viewer_current_asset", auth, timeout=8)
        if not response["ok"]:
            return result_error(host, "v1", response)
        asset = response.get("data")

    if not isinstance(asset, dict) or not asset.get("asset_id"):
        return {"host": host, "ok": True, "asset": None}

    with CURRENT_ASSET_LOCK:
        CURRENT_ASSET_CACHE[host] = dict(asset)

    result = {"host": host, "ok": True, "asset": asset}
    if telemetry.get("ok") and str(telemetry.get("assetId")) == str(asset.get("asset_id")):
        result["telemetry"] = {
            "position": telemetry.get("position"),
            "duration": telemetry.get("duration"),
            "assetId": telemetry.get("assetId"),
        }
    if str(asset.get("mimetype", "")).lower().startswith("image"):
        asset_id = quote(str(asset["asset_id"]), safe="")
        content = screenly_request("GET", host, f"/api/v1/assets/{asset_id}/content", auth, timeout=20)
        payload = content.get("data")
        if content["ok"] and isinstance(payload, dict) and payload.get("type") == "file" and payload.get("content"):
            mimetype = payload.get("mimetype") or "image/jpeg"
            result["preview"] = f"data:{mimetype};base64,{payload['content']}"
        elif content["ok"] and isinstance(payload, dict) and payload.get("type") == "url":
            result["previewUrl"] = payload.get("url")
    return result


@app.get("/api/live-media/<host>/<asset_id>")
def live_media(host, asset_id):
    host = clean_hosts([host])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", asset_id):
        return jsonify(error="ID de contenido no valido"), 400
    config = load_monitor_config()
    if not config.get("token"):
        return jsonify(error="Monitor de reproduccion no configurado"), 503
    headers = {"X-Fleet-Token": config["token"]}
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    try:
        upstream = requests.get(
            f"http://{host}:{config['port']}/media/{quote(asset_id, safe='')}",
            headers=headers, stream=True, timeout=(5, 30),
        )
    except requests.RequestException as error:
        return jsonify(error=str(error)), 502

    forwarded = {}
    for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Cache-Control"):
        if upstream.headers.get(header):
            forwarded[header] = upstream.headers[header]

    def generate():
        try:
            for chunk in upstream.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(stream_with_context(generate()), status=upstream.status_code, headers=forwarded)


@app.post("/api/upload")
def upload_fleet():
    video = request.files.get("video")
    if video is None or not video.filename:
        return jsonify(error="Selecciona un video"), 400

    hosts = clean_hosts(request.form.get("hosts"))
    auth = auth_from(request.form)
    preferred = request.form.get("apiVersion", "auto")
    content = video.read()
    filename = Path(video.filename).name
    name = request.form.get("name", "").strip() or Path(filename).stem
    start_date = iso_date(request.form.get("startDate", "now"))
    end_date = iso_date(request.form.get("endDate", "9999-01-01T00:00:00Z"))
    duration = max(0, int(request.form.get("duration", 0)))
    enabled = request.form.get("enabled", "1") == "1"
    skip_check = request.form.get("skipAssetCheck", "1") == "1"

    results = []
    for host in hosts:
        detected = detect_api(host, auth, preferred)
        if not detected["ok"]:
            results.append({"host": host, "ok": False, "error": detected["error"]})
            continue

        version = detected["version"]
        upload_path = "/api/v2/file_asset" if version == "v2" else "/api/v1/file_asset"
        uploaded = screenly_request(
            "POST", host, upload_path, auth,
            files={"file_upload": (filename, content, video.mimetype or "video/mp4")},
            timeout=600,
        )
        if not uploaded["ok"]:
            results.append(result_error(host, version, uploaded))
            continue

        upload_data = uploaded.get("data", {})
        uri = upload_data.get("uri", "") if isinstance(upload_data, dict) else ""
        if not uri:
            results.append({"host": host, "ok": False, "version": version, "error": "La subida no devolvio una URI"})
            continue

        content_kind = "image" if (video.mimetype or "").startswith("image/") else "video"
        payload = {
            "name": name,
            "uri": uri,
            "start_date": start_date,
            "end_date": end_date,
            "duration": duration,
            "mimetype": upload_data.get("mimetype", content_kind),
            "is_enabled": enabled if version == "v2" else int(enabled),
            "skip_asset_check": skip_check if version == "v2" else int(skip_check),
        }
        if version == "v2":
            payload.update({
                "ext": upload_data.get("ext", Path(filename).suffix.lstrip(".")),
                "is_processing": True,
                "nocache": False,
                "play_order": 0,
            })

        asset_path = "/api/v2/assets" if version == "v2" else "/api/v1.2/assets"
        created = screenly_request("POST", host, asset_path, auth, json=payload)
        results.append({
            "host": host,
            "ok": created["ok"],
            "version": version,
            "asset": created.get("data"),
            "error": None if created["ok"] else created["error"],
        })
    return jsonify(results=results)


@app.post("/api/url")
def create_url_asset():
    data = request.get_json(silent=True) or {}
    hosts = clean_hosts(data.get("hosts"))
    auth = auth_from(data)
    preferred = data.get("apiVersion", "auto")
    uri = str(data.get("url", "")).strip()
    if not uri.startswith(("http://", "https://")):
        return jsonify(error="La direccion web debe comenzar por http:// o https://"), 400

    name = str(data.get("name", "")).strip() or uri
    payload_base = {
        "name": name,
        "uri": uri,
        "start_date": iso_date(str(data.get("startDate", "now"))),
        "end_date": iso_date(str(data.get("endDate", "9999-01-01T00:00:00Z"))),
        "duration": max(0, int(data.get("duration", 0))),
        "mimetype": "webpage",
        "is_enabled": bool(data.get("enabled", True)),
        "skip_asset_check": True,
    }
    results = []
    for host in hosts:
        detected = detect_api(host, auth, preferred)
        if not detected["ok"]:
            results.append({"host": host, "ok": False, "error": detected["error"]})
            continue
        version = detected["version"]
        payload = dict(payload_base)
        if version == "v2":
            payload.update({"is_processing": False, "nocache": False, "play_order": 0})
        else:
            payload["is_enabled"] = int(payload["is_enabled"])
            payload["skip_asset_check"] = 1
        path = "/api/v2/assets" if version == "v2" else "/api/v1.2/assets"
        created = screenly_request("POST", host, path, auth, json=payload)
        results.append({
            "host": host,
            "ok": created["ok"],
            "version": version,
            "asset": created.get("data"),
            "error": None if created["ok"] else created["error"],
        })
    return jsonify(results=results)


@app.post("/api/asset")
def asset_action():
    data = request.get_json(silent=True) or {}
    auth = auth_from(data)
    operation = data.get("operation", "")
    preferred = data.get("apiVersion", "auto")
    targets = clean_targets(data)
    if operation not in {"enable", "disable", "delete", "update"}:
        return jsonify(error="Operacion no valida"), 400
    update = data.get("update") if isinstance(data.get("update"), dict) else {}

    results = []
    for target in targets:
        host = target["host"]
        asset_id = target["assetId"]
        detected = detect_api(host, auth, preferred)
        if not detected["ok"]:
            results.append({"host": host, "ok": False, "error": detected["error"]})
            continue
        version = detected["version"]
        prefix = "/api/v2/assets/" if version == "v2" else "/api/v1.2/assets/"
        path = prefix + quote(asset_id, safe="")
        if operation == "delete":
            response = screenly_request("DELETE", host, path, auth)
        elif operation == "update":
            changes = normalize_update(update, version)
            response = screenly_request("PATCH", host, path, auth, json=changes)
            if not response["ok"] and version == "v1.2":
                current = find_asset(detected.get("assets", []), asset_id)
                if current:
                    full_asset = dict(current)
                    full_asset.update(changes)
                    response = screenly_request("PUT", host, path, auth, json=full_asset)
        else:
            value = operation == "enable"
            response = screenly_request("PATCH", host, path, auth, json={"is_enabled": value})
            if not response["ok"] and version == "v1.2":
                current = find_asset(detected.get("assets", []), asset_id)
                body = dict(current) if current else {}
                body["is_enabled"] = int(value)
                response = screenly_request("PUT", host, path, auth, json=body)
        results.append({
            "host": host,
            "ok": response["ok"],
            "version": version,
            "error": None if response["ok"] else response["error"],
        })
    return jsonify(results=results)


@app.post("/api/download")
def download_asset():
    data = request.get_json(silent=True) or {}
    targets = clean_targets(data)
    if len(targets) != 1:
        return jsonify(error="Selecciona un unico contenido para descargar"), 400
    target = targets[0]
    auth = auth_from(data)
    path = f"/api/v1/assets/{quote(target['assetId'], safe='')}/content"
    response = screenly_request("GET", target["host"], path, auth, timeout=600)
    if not response["ok"]:
        return jsonify(error=response["error"]), response.get("status") or 502

    content = response.get("data")
    if not isinstance(content, dict):
        return jsonify(error="Screenly no devolvio un contenido descargable"), 502
    if content.get("type") == "url":
        return jsonify(type="url", url=content.get("url", ""))
    if content.get("type") != "file" or not content.get("content"):
        return jsonify(error="El contenido no es un archivo descargable"), 404

    try:
        binary = base64.b64decode(content["content"], validate=True)
    except (ValueError, TypeError) as error:
        return jsonify(error=f"El archivo recibido no es valido: {error}"), 502
    filename = Path(str(content.get("filename") or f"asset-{target['assetId']}" )).name
    return send_file(
        BytesIO(binary),
        mimetype=content.get("mimetype") or "application/octet-stream",
        as_attachment=True,
        download_name=filename,
    )


@app.post("/api/control")
def control_playback():
    data = request.get_json(silent=True) or {}
    hosts = clean_hosts(data.get("hosts"))
    if len(hosts) != 1:
        return jsonify(error="El control de reproduccion requiere una sola pantalla"), 400
    direction = str(data.get("direction", ""))
    if direction not in {"previous", "next"}:
        return jsonify(error="Control de reproduccion no valido"), 400
    auth = auth_from(data)
    response = screenly_request("GET", hosts[0], f"/api/v1/assets/control/{direction}", auth)
    if not response["ok"]:
        return jsonify(error=response["error"]), response.get("status") or 502
    return jsonify(ok=True, host=hosts[0], direction=direction)


@app.post("/api/order")
def update_playlist_order():
    data = request.get_json(silent=True) or {}
    hosts = clean_hosts(data.get("hosts"))
    if len(hosts) != 1:
        return jsonify(error="El orden solo puede cambiarse en una pantalla cada vez"), 400
    ordered_ids = data.get("orderedIds")
    if not isinstance(ordered_ids, list) or not ordered_ids:
        return jsonify(error="No hay contenidos para ordenar"), 400
    ordered_ids = [str(asset_id).strip() for asset_id in ordered_ids]
    if any(not asset_id for asset_id in ordered_ids) or len(set(ordered_ids)) != len(ordered_ids):
        return jsonify(error="La lista de contenidos contiene IDs no validos"), 400

    auth = auth_from(data)
    detected = detect_api(hosts[0], auth, data.get("apiVersion", "auto"))
    if not detected["ok"]:
        return jsonify(error=detected["error"]), detected.get("status") or 502
    known_ids = {
        str(asset.get("asset_id", asset.get("assetId", asset.get("id", ""))))
        for asset in detected.get("assets", []) if isinstance(asset, dict)
    }
    unknown = [asset_id for asset_id in ordered_ids if asset_id not in known_ids]
    if unknown:
        return jsonify(error=f"Contenido no encontrado: {unknown[0]}"), 409
    if detected["version"] != "v1.2":
        return jsonify(error="Esta version de Screenly no expone el orden de playlist compatible"), 400

    response = screenly_request(
        "POST", hosts[0], "/api/v1/assets/order", auth,
        data={"ids": ",".join(ordered_ids)},
    )
    if not response["ok"]:
        return jsonify(error=response["error"]), response.get("status") or 502
    return jsonify(ok=True, host=hosts[0], orderedIds=ordered_ids)


@app.errorhandler(Exception)
def handle_error(error):
    app.logger.exception(error)
    return jsonify(error=str(error)), 500


@app.errorhandler(ValueError)
def handle_value_error(error):
    return jsonify(error=str(error)), 400


def load_fleet():
    if not CONFIG_PATH.exists():
        return [dict(item) for item in DEFAULT_FLEET]
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [dict(item) for item in DEFAULT_FLEET]
    if not isinstance(data, list):
        return [dict(item) for item in DEFAULT_FLEET]
    fleet = []
    seen = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            host = validate_private_host(item.get("host"))
        except ValueError:
            continue
        if host in seen:
            continue
        seen.add(host)
        fleet.append({"host": host, "name": clean_host_name(item.get("name"), host)})
    return fleet


def save_fleet(fleet):
    temporary = CONFIG_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(fleet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, CONFIG_PATH)


def validate_private_host(value):
    host = str(value or "").strip()
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("Introduce una direccion IP valida") from error
    if address.version != 4 or not address.is_private or address.is_loopback or address.is_multicast or address.is_unspecified:
        raise ValueError("Solo se permiten direcciones IPv4 privadas de la red local")
    return str(address)


def clean_host_name(value, host):
    name = " ".join(str(value or "").strip().split())
    return name[:60] or f"Raspberry {host.split('.')[-1]}"


def fleet_hosts():
    return [item["host"] for item in load_fleet()]


def clean_hosts(value):
    if value is None:
        hosts = fleet_hosts()
    elif isinstance(value, str):
        hosts = [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    elif isinstance(value, list):
        hosts = [str(part).strip() for part in value if str(part).strip()]
    else:
        raise ValueError("Lista de IP no valida")
    allowed_hosts = set(fleet_hosts())
    invalid = [host for host in hosts if host not in allowed_hosts]
    if invalid:
        raise ValueError(f"IP no permitida: {invalid[0]}")
    return list(dict.fromkeys(hosts))


def clean_targets(data):
    raw_targets = data.get("targets")
    if raw_targets is None:
        asset_id = str(data.get("assetId", "")).strip()
        raw_targets = [{"host": host, "assetId": asset_id} for host in clean_hosts(data.get("hosts"))]
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("No hay contenidos seleccionados")

    targets = []
    seen = set()
    for target in raw_targets:
        if not isinstance(target, dict):
            raise ValueError("Destino no valido")
        host = str(target.get("host", "")).strip()
        asset_id = str(target.get("assetId", "")).strip()
        if host not in set(fleet_hosts()):
            raise ValueError(f"IP no permitida: {host}")
        if not asset_id:
            raise ValueError("Falta el ID del contenido")
        key = (host, asset_id)
        if key not in seen:
            seen.add(key)
            targets.append({"host": host, "assetId": asset_id})
    return targets


def normalize_update(update, version):
    allowed = {"name", "start_date", "end_date", "duration", "is_enabled"}
    changes = {key: value for key, value in update.items() if key in allowed}
    if "start_date" in changes:
        changes["start_date"] = iso_date(str(changes["start_date"]))
    if "end_date" in changes:
        changes["end_date"] = iso_date(str(changes["end_date"]))
    if "duration" in changes:
        changes["duration"] = max(0, int(changes["duration"]))
    if "is_enabled" in changes:
        changes["is_enabled"] = bool(changes["is_enabled"]) if version == "v2" else int(bool(changes["is_enabled"]))
    return changes


def find_asset(assets, wanted_id):
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        current_id = asset.get("asset_id", asset.get("assetId", asset.get("id", "")))
        if str(current_id) == str(wanted_id):
            return asset
    return None


def auth_from(data):
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    return (username, password) if username else None


def load_monitor_config():
    try:
        data = json.loads(MONITOR_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"token": "", "port": 8765}
    return {
        "token": str(data.get("token", "")),
        "port": max(1, min(65535, int(data.get("port", 8765)))),
    }


def monitor_status(host):
    config = load_monitor_config()
    if not config["token"]:
        return {"ok": False}
    try:
        response = requests.get(
            f"http://{host}:{config['port']}/status",
            headers={"X-Fleet-Token": config["token"]}, timeout=(2, 3),
        )
        data = response.json() if response.ok else {}
        return data if isinstance(data, dict) else {"ok": False}
    except (requests.RequestException, ValueError):
        return {"ok": False}


def screenly_request(method, host, path, auth, timeout=120, **kwargs):
    try:
        response = requests.request(
            method, f"http://{host}{path}", auth=auth,
            timeout=(8, timeout), headers={"Accept": "application/json"}, **kwargs,
        )
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text}
        if not response.ok:
            message = data.get("error") or data.get("message") or response.text if isinstance(data, dict) else response.text
            return {"ok": False, "status": response.status_code, "error": message or "Error de Screenly", "data": data}
        return {"ok": True, "status": response.status_code, "data": data}
    except requests.RequestException as error:
        return {"ok": False, "status": 0, "error": str(error)}


def detect_api(host, auth, preferred="auto"):
    versions = [preferred] if preferred in {"v2", "v1.2"} else ["v2", "v1.2"]
    last_error = "No responde la API de Screenly/Anthias"
    last_status = 0
    for version in versions:
        path = "/api/v2/assets" if version == "v2" else "/api/v1.2/assets"
        response = screenly_request("GET", host, path, auth)
        last_status = response.get("status", 0)
        if response["ok"]:
            assets = normalize_assets(response.get("data"))
            if assets is not None:
                return {"ok": True, "version": version, "assets": assets, "status": last_status}
            last_error = f"La API {version} no devolvio una lista de contenidos"
            continue
        last_error = response.get("error", last_error)
    return {"ok": False, "version": None, "assets": [], "error": last_error, "status": last_status}


def normalize_assets(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("assets", "data", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        values = list(data.values())
        if values and all(isinstance(item, dict) for item in values):
            return values
    return None


def list_host(host, auth, preferred):
    detected = detect_api(host, auth, preferred)
    return {
        "host": host,
        "ok": detected["ok"],
        "version": detected["version"],
        "assets": detected["assets"],
        "error": detected.get("error"),
        "status": detected.get("status", 0),
        "authRequired": detected.get("status") in {401, 403},
    }


def result_error(host, version, response):
    return {"host": host, "ok": False, "version": version, "error": response["error"]}


def iso_date(value):
    if not value or value == "now":
        date = datetime.now(timezone.utc)
    else:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        date = date.astimezone(timezone.utc)
    return date.isoformat(timespec="milliseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    url = "http://127.0.0.1:5000"
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Panel Screenly disponible en {url}")
    print("Para cerrarlo, cierra esta ventana o pulsa Ctrl+C.")
    app.run(host="127.0.0.1", port=5000, debug=False)
