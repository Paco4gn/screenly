import os
import sys
import webbrowser
import base64
import hmac
import ipaddress
import json
import re
import secrets
import tempfile
import time
from io import BytesIO
from concurrent.futures import TimeoutError as FuturesTimeout
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Timer
from urllib.parse import quote, urlsplit

BASE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = BASE_DIR / "vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

from flask import Flask, Response, jsonify, redirect, request, send_file, send_from_directory, session, stream_with_context
import requests
from werkzeug.exceptions import Forbidden, HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

CONFIG_PATH = BASE_DIR / "fleet.json"
MONITOR_CONFIG_PATH = BASE_DIR / "monitor.json"
HISTORY_PATH = BASE_DIR / "history.jsonl"
SETTINGS_PATH = BASE_DIR / "settings.json"
USERS_PATH = BASE_DIR / "users.json"
CONFIG_LOCK = Lock()
SETTINGS_LOCK = Lock()
USERS_LOCK = Lock()
HISTORY_LOCK = Lock()
CURRENT_ASSET_LOCK = Lock()
CURRENT_ASSET_CACHE = {}
MAX_FLEET_WORKERS = 10
MAX_UPLOAD_WORKERS = 1
UPLOAD_RETRIES = 2
MAX_ASSET_NAME_LENGTH = 160
MAX_HISTORY_ITEMS = 250
APP_EMAIL = os.environ.get("CENTRO_MANDO_EMAIL", "informatica@feval.com").strip().lower()
APP_PASSWORD_HASH = os.environ.get(
    "CENTRO_MANDO_PASSWORD_HASH",
    "scrypt:32768:8:1$jf3kDGJdwS29aU2m$523ff11db401535be0ac178470be644ed5d99a30c5fba71478e37ed41e1aa21efdd180f1329fc1dee3977017b5080784fae37b912c971b50ea0216627c7d01c4",
)
APP_ROLE = os.environ.get("CENTRO_MANDO_ROLE", "admin").strip().lower()
if APP_ROLE not in {"admin", "operator", "viewer"}:
    APP_ROLE = "admin"
ROLE_LEVELS = {"viewer": 1, "operator": 2, "admin": 3}
LOGIN_ATTEMPTS = {}
LOGIN_LOCK = Lock()
LOGIN_MAX_ATTEMPTS = 5
LOGIN_BLOCK_SECONDS = 300
DEFAULT_FLEET = [
    {"host": f"192.168.20.{number}", "name": f"Pantalla {index}"}
    for index, number in enumerate(range(223, 229), start=1)
]


def load_or_create_session_secret():
    path = BASE_DIR / ".session_secret"
    try:
        secret = path.read_text(encoding="utf-8").strip()
        if len(secret) >= 32:
            return secret
    except OSError:
        pass
    secret = secrets.token_urlsafe(48)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
            secret_file.write(secret + "\n")
        return secret
    except FileExistsError:
        stored = path.read_text(encoding="utf-8").strip()
        if len(stored) >= 32:
            return stored
        raise RuntimeError("El secreto de sesion no es valido")


def login_blocked_seconds(client):
    now = time.monotonic()
    with LOGIN_LOCK:
        attempt = LOGIN_ATTEMPTS.get(client)
        if not attempt:
            return 0
        remaining = int(attempt.get("blocked_until", 0) - now)
        if remaining <= 0 and attempt.get("blocked_until"):
            LOGIN_ATTEMPTS.pop(client, None)
            return 0
        return max(0, remaining)


def register_failed_login(client):
    now = time.monotonic()
    with LOGIN_LOCK:
        attempt = LOGIN_ATTEMPTS.setdefault(client, {"count": 0, "blocked_until": 0})
        attempt["count"] += 1
        if attempt["count"] >= LOGIN_MAX_ATTEMPTS:
            attempt["blocked_until"] = now + LOGIN_BLOCK_SECONDS
            return True
    return False


def require_role(required):
    current = session.get("role", "viewer")
    if ROLE_LEVELS.get(current, 0) < ROLE_LEVELS[required]:
        raise Forbidden("Tu usuario no tiene permisos para esta accion")


def normalize_email(value):
    return " ".join(str(value or "").strip().lower().split())[:160]


def bootstrap_user():
    return {
        "email": APP_EMAIL,
        "passwordHash": APP_PASSWORD_HASH,
        "role": APP_ROLE,
        "active": True,
        "createdAt": "bootstrap",
    }


def clean_user_record(user):
    if not isinstance(user, dict):
        return None
    email = normalize_email(user.get("email"))
    password_hash = str(user.get("passwordHash", ""))
    role = str(user.get("role", "viewer")).strip().lower()
    if role not in ROLE_LEVELS:
        role = "viewer"
    if not email or not password_hash:
        return None
    return {
        "email": email,
        "passwordHash": password_hash,
        "role": role,
        "active": bool(user.get("active", True)),
        "createdAt": str(user.get("createdAt", "")),
    }


def load_users():
    try:
        data = json.loads(USERS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    raw_users = data.get("users") if isinstance(data, dict) else None
    users = []
    seen = set()
    for raw in raw_users or []:
        user = clean_user_record(raw)
        if user and user["email"] not in seen:
            users.append(user)
            seen.add(user["email"])
    if not users:
        users.append(bootstrap_user())
    return users


def save_users(users):
    cleaned = []
    seen = set()
    for raw in users:
        user = clean_user_record(raw)
        if user and user["email"] not in seen:
            cleaned.append(user)
            seen.add(user["email"])
    if not any(user["active"] and user["role"] == "admin" for user in cleaned):
        raise ValueError("Debe quedar al menos un administrador activo")
    temporary = USERS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"users": cleaned}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, USERS_PATH)


def public_user(user):
    return {
        "email": user["email"],
        "role": user["role"],
        "active": bool(user.get("active", True)),
        "createdAt": user.get("createdAt", ""),
    }


def find_user(email):
    normalized = normalize_email(email)
    return next((user for user in load_users() if user["email"] == normalized), None)


def authenticate_user(email, password):
    normalized = normalize_email(email)
    for user in load_users():
        if hmac.compare_digest(user["email"], normalized) and user.get("active", True):
            if check_password_hash(user["passwordHash"], password):
                return user
            return None
    return None


def current_user():
    email = session.get("authenticated_email")
    if not email:
        return None
    user = find_user(email)
    if not user or not user.get("active", True):
        return None
    return user


app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("CENTRO_MANDO_SECRET_KEY") or load_or_create_session_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("CENTRO_MANDO_HTTPS") == "1",
    PERMANENT_SESSION_LIFETIME=28800,
)


@app.before_request
def require_application_login():
    if request.path in {"/login", "/api/login", "/favicon.ico", "/favicon-32.png", "/favicon-192.png", "/login.css", "/login.js"}:
        return None
    user = current_user()
    if user:
        session["role"] = user["role"]
        return None
    session.clear()
    if request.path.startswith("/api/"):
        return jsonify(error="Inicia sesion para continuar"), 401
    return redirect("/login")


@app.after_request
def secure_local_response(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path.startswith("/api/") or request.path in {"/", "/login", "/app.js", "/styles.css", "/login.js", "/login.css"}:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/login")
def login_page():
    if current_user():
        return redirect("/")
    return send_from_directory(BASE_DIR, "login.html")


@app.post("/api/login")
def login_api():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    client = request.remote_addr or "local"
    blocked_for = login_blocked_seconds(client)
    if blocked_for:
        return jsonify(error=f"Demasiados intentos. Espera {blocked_for} segundos."), 429

    user = authenticate_user(email, password)
    if not user:
        blocked = register_failed_login(client)
        status = 429 if blocked else 401
        message = "Demasiados intentos. Espera 5 minutos." if blocked else "Correo o contrasena incorrectos"
        return jsonify(error=message), status

    with LOGIN_LOCK:
        LOGIN_ATTEMPTS.pop(client, None)
    session.clear()
    session.permanent = True
    session["authenticated_email"] = user["email"]
    session["role"] = user["role"]
    return jsonify(ok=True, role=user["role"])


@app.post("/api/logout")
def logout_api():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/session")
def session_api():
    return jsonify(email=session.get("authenticated_email"), role=session.get("role", "viewer"))


@app.get("/api/users")
def get_users():
    require_role("admin")
    return jsonify(users=[public_user(user) for user in load_users()], roles=list(ROLE_LEVELS.keys()))


@app.post("/api/users")
def create_user():
    require_role("admin")
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email"))
    password = str(data.get("password", ""))
    role = str(data.get("role", "viewer")).strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ValueError("Introduce un correo valido")
    if len(password) < 8:
        raise ValueError("La contrasena debe tener al menos 8 caracteres")
    if role not in ROLE_LEVELS:
        raise ValueError("Rol no valido")
    with USERS_LOCK:
        users = load_users()
        if any(user["email"] == email for user in users):
            raise ValueError("Ya existe una cuenta con ese correo")
        user = {
            "email": email,
            "passwordHash": generate_password_hash(password),
            "role": role,
            "active": bool(data.get("active", True)),
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        users.append(user)
        save_users(users)
    append_history("user_created", None, f"Usuario creado: {email}", f"Rol {role}")
    return jsonify(user=public_user(user)), 201


@app.patch("/api/users/<path:email>")
def update_user(email):
    require_role("admin")
    data = request.get_json(silent=True) or {}
    wanted = normalize_email(email)
    role = data.get("role")
    password = data.get("password")
    with USERS_LOCK:
        users = load_users()
        user = next((item for item in users if item["email"] == wanted), None)
        if not user:
            return jsonify(error="Usuario no encontrado"), 404
        if role is not None:
            role = str(role).strip().lower()
            if role not in ROLE_LEVELS:
                raise ValueError("Rol no valido")
            user["role"] = role
        if "active" in data:
            user["active"] = bool(data.get("active"))
        if password:
            password = str(password)
            if len(password) < 8:
                raise ValueError("La contrasena debe tener al menos 8 caracteres")
            user["passwordHash"] = generate_password_hash(password)
        save_users(users)
    if session.get("authenticated_email") == wanted:
        session["role"] = user["role"]
    append_history("user_updated", None, f"Usuario actualizado: {wanted}", f"Rol {user['role']}")
    return jsonify(user=public_user(user))


@app.delete("/api/users/<path:email>")
def delete_user(email):
    require_role("admin")
    wanted = normalize_email(email)
    if wanted == session.get("authenticated_email"):
        raise ValueError("No puedes eliminar tu propia cuenta iniciada")
    with USERS_LOCK:
        users = load_users()
        remaining = [user for user in users if user["email"] != wanted]
        if len(remaining) == len(users):
            return jsonify(error="Usuario no encontrado"), 404
        save_users(remaining)
    append_history("user_deleted", None, f"Usuario eliminado: {wanted}")
    return jsonify(ok=True)


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/<path:filename>")
def static_file(filename):
    if filename not in {"app.js", "styles.css", "favicon.ico", "favicon-32.png", "favicon-192.png", "login.css", "login.js"}:
        return jsonify(error="Archivo no permitido"), 404
    return send_from_directory(BASE_DIR, filename)


@app.get("/api/hosts")
def get_hosts():
    return jsonify(hosts=[public_host(item) for item in load_fleet()])


@app.get("/api/settings")
def get_settings():
    return jsonify(settings=public_settings(load_settings()))


@app.patch("/api/settings")
def update_settings():
    require_role("admin")
    data = request.get_json(silent=True) or {}
    with SETTINGS_LOCK:
        settings = load_settings()
        if "defaultAuth" in data and isinstance(data.get("defaultAuth"), dict):
            settings["defaultAuth"] = clean_auth_config(data["defaultAuth"], settings.get("defaultAuth"))
        save_settings(settings)
    append_history("settings_updated", None, "Credenciales globales actualizadas")
    return jsonify(settings=public_settings(settings))


@app.get("/api/history")
def get_history():
    return jsonify(events=load_history())


@app.get("/api/health")
def health():
    return jsonify(ok=True, service="fleetboard", screens=len(load_fleet()))


@app.post("/api/hosts")
def add_host():
    require_role("admin")
    data = request.get_json(silent=True) or {}
    host = validate_private_host(data.get("host"))
    item = clean_host_config(data, host)
    item["name"] = clean_host_name(data.get("name"), host)
    with CONFIG_LOCK:
        fleet = load_fleet()
        if any(item["host"] == host for item in fleet):
            return jsonify(error="Esa Raspberry ya esta en la flota"), 409
        fleet.append(item)
        save_fleet(fleet)
    append_history("host_added", host, f"Raspberry anadida: {item['name']}")
    return jsonify(host=public_host(item)), 201


@app.patch("/api/hosts/<host>")
def update_host(host):
    require_role("admin")
    host = validate_private_host(host)
    data = request.get_json(silent=True) or {}
    with CONFIG_LOCK:
        fleet = load_fleet()
        item = next((item for item in fleet if item["host"] == host), None)
        if item is None:
            return jsonify(error="La Raspberry no existe en la flota"), 404
        if "name" in data:
            item["name"] = clean_host_name(data.get("name"), host)
        if "auth" in data and isinstance(data.get("auth"), dict):
            item["auth"] = clean_auth_config(data["auth"], item.get("auth"))
        if "maintenance" in data:
            item["maintenance"] = bool(data.get("maintenance"))
        if "notes" in data:
            item["notes"] = clean_notes(data.get("notes"))
        save_fleet(fleet)
    append_history("host_updated", host, f"Configuracion actualizada: {item['name']}")
    return jsonify(host=public_host(item))


@app.delete("/api/hosts/<host>")
def remove_host(host):
    require_role("admin")
    host = validate_private_host(host)
    with CONFIG_LOCK:
        fleet = load_fleet()
        updated = [item for item in fleet if item["host"] != host]
        if len(updated) == len(fleet):
            return jsonify(error="La Raspberry no existe en la flota"), 404
        save_fleet(updated)
    append_history("host_removed", host, "Raspberry eliminada del panel")
    return jsonify(ok=True, host=host)


@app.post("/api/list")
def list_fleet():
    data = request.get_json(silent=True) or {}
    hosts = clean_hosts(data.get("hosts"))
    preferred = data.get("apiVersion", "auto")
    results = parallel_map(
        hosts,
        lambda host: list_host(host, auth_for_host(host, data), api_preference_for_host(host, preferred)),
    )
    return jsonify(results=results)


@app.post("/api/diagnostics")
def diagnostics_fleet():
    data = request.get_json(silent=True) or {}
    hosts = clean_hosts(data.get("hosts"))
    preferred = data.get("apiVersion", "auto")
    results = parallel_map(hosts, lambda host: diagnose_host(host, auth_for_host(host, data), preferred))
    return jsonify(results=results, checkedAt=datetime.now(timezone.utc).isoformat())


@app.post("/api/now")
def now_playing_fleet():
    data = request.get_json(silent=True) or {}
    hosts = clean_hosts(data.get("hosts"))
    results = parallel_map_timeout(
        hosts,
        lambda host: current_asset(host, auth_for_host(host, data)),
        timeout=6,
        on_timeout=lambda host: {
            "host": host,
            "ok": False,
            "error": "La Raspberry tarda demasiado en responder",
            "monitor": {"connected": False, "reason": "timeout"},
        },
        on_error=lambda host, error: {
            "host": host,
            "ok": False,
            "error": str(error) or "No se pudo consultar la Raspberry",
            "monitor": {"connected": False, "reason": "error"},
        },
    )
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
            response = screenly_request("GET", host, "/api/v1.2/assets", auth, timeout=4, connect_timeout=1.5)
            assets = normalize_assets(response.get("data")) if response["ok"] else None
            asset = find_asset(assets or [], telemetry["assetId"])
    else:
        response = screenly_request("GET", host, "/api/v1/viewer_current_asset", auth, timeout=4, connect_timeout=1.5)
        if not response["ok"]:
            error = result_error(host, "v1", response)
            error["monitor"] = monitor_summary(telemetry)
            return error
        asset = response.get("data")

    if not isinstance(asset, dict) or not asset.get("asset_id"):
        return {"host": host, "ok": True, "asset": None, "monitor": monitor_summary(telemetry)}

    with CURRENT_ASSET_LOCK:
        CURRENT_ASSET_CACHE[host] = dict(asset)

    result = {"host": host, "ok": True, "asset": asset, "monitor": monitor_summary(telemetry)}
    if telemetry.get("ok") and str(telemetry.get("assetId")) == str(asset.get("asset_id")):
        result["telemetry"] = {
            "position": telemetry.get("position"),
            "duration": telemetry.get("duration"),
            "assetId": telemetry.get("assetId"),
        }
    if str(asset.get("mimetype", "")).lower().startswith("image"):
        result["previewStream"] = f"/api/live-media/{quote(host, safe='')}/{quote(str(asset['asset_id']), safe='')}"
    return result


@app.get("/api/live-media/<host>/<asset_id>")
def live_media(host, asset_id):
    host = clean_hosts([host])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", asset_id):
        return jsonify(error="ID de contenido no valido"), 400
    config = monitor_credentials(host)
    if not config["token"]:
        return jsonify(error="Monitor de reproduccion no configurado"), 503
    headers = {"X-Fleet-Token": config["token"]}
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    try:
        upstream = requests.get(
            f"http://{host}:{config['port']}/media/{quote(asset_id, safe='')}",
            headers=headers, stream=True, timeout=(2, 12),
        )
    except requests.RequestException as error:
        return jsonify(error=str(error)), 502

    first_chunk = b""
    try:
        iterator = upstream.iter_content(65536)
        first_chunk = next((chunk for chunk in iterator if chunk), b"")
    except requests.RequestException as error:
        upstream.close()
        return jsonify(error=str(error)), 502

    forwarded = {}
    for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Cache-Control"):
        if upstream.headers.get(header):
            forwarded[header] = upstream.headers[header]
    detected_type = sniff_media_type(first_chunk)
    if detected_type:
        forwarded["Content-Type"] = detected_type

    def generate():
        try:
            if first_chunk:
                yield first_chunk
            for chunk in iterator:
                if chunk:
                    yield chunk
        except requests.RequestException:
            app.logger.warning("Corte leyendo media de %s/%s", host, asset_id, exc_info=True)
        finally:
            upstream.close()

    return Response(stream_with_context(generate()), status=upstream.status_code, headers=forwarded)


@app.get("/api/asset-media/<host>/<asset_id>")
def asset_media(host, asset_id):
    host = clean_hosts([host])[0]
    auth = auth_for_host(host, {})
    path = f"/api/v1/assets/{quote(asset_id, safe='')}/content"
    response = screenly_request("GET", host, path, auth, timeout=5, connect_timeout=1.5)
    if not response["ok"]:
        return jsonify(error=response["error"]), response.get("status") or 502

    content = response.get("data")
    if not isinstance(content, dict):
        return jsonify(error="Screenly no devolvio un contenido visualizable"), 502
    if content.get("type") == "url":
        url = str(content.get("url", "")).strip()
        if not url:
            return jsonify(error="El contenido remoto no incluye una direccion valida"), 404
        return redirect(url)
    if content.get("type") != "file" or not content.get("content"):
        return jsonify(error="El contenido no es un archivo visualizable"), 404

    try:
        binary = base64.b64decode(content["content"], validate=True)
    except (ValueError, TypeError) as error:
        return jsonify(error=f"El archivo recibido no es valido: {error}"), 502

    filename = Path(str(content.get("filename") or f"asset-{asset_id}")).name
    return send_file(
        BytesIO(binary),
        mimetype=content.get("mimetype") or "application/octet-stream",
        as_attachment=False,
        download_name=filename,
        max_age=60,
    )


@app.post("/api/upload")
def upload_fleet():
    require_role("operator")
    video = request.files.get("video")
    if video is None or not video.filename:
        return jsonify(error="Selecciona un video o una imagen"), 400

    hosts = clean_hosts(request.form.get("hosts"))
    form_data = request.form.to_dict(flat=True)
    preferred = request.form.get("apiVersion", "auto")
    filename = Path(video.filename).name
    name = clean_asset_name(request.form.get("name"), Path(filename).stem)
    start_date, end_date = schedule_dates(
        request.form.get("startDate", "now"),
        request.form.get("endDate", "9999-01-01T00:00:00Z"),
    )
    duration = max(0, int(request.form.get("duration", 0)))
    enabled = request.form.get("enabled", "1") == "1"
    skip_check = request.form.get("skipAssetCheck", "1") == "1"
    duplicate_policy = request.form.get("duplicatePolicy", "skip")
    mimetype = video.mimetype or "application/octet-stream"
    suffix = Path(filename).suffix[:12]
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="fleetboard-", suffix=suffix, delete=False) as temporary:
            temporary_path = temporary.name
            video.save(temporary)
        if os.path.getsize(temporary_path) == 0:
            raise ValueError("El archivo seleccionado esta vacio")
        worker = lambda host: upload_to_host(
            host, temporary_path, filename, mimetype, name, start_date, end_date,
            duration, enabled, skip_check, duplicate_policy,
            auth_for_host(host, form_data), api_preference_for_host(host, preferred),
        )
        results = parallel_map(hosts, worker, MAX_UPLOAD_WORKERS)
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
    return jsonify(results=results)


@app.post("/api/url")
def create_url_asset():
    require_role("operator")
    data = request.get_json(silent=True) or {}
    hosts = clean_hosts(data.get("hosts"))
    preferred = data.get("apiVersion", "auto")
    uri = str(data.get("url", "")).strip()
    parsed_uri = urlsplit(uri)
    if parsed_uri.scheme not in {"http", "https"} or not parsed_uri.netloc:
        return jsonify(error="La direccion web debe comenzar por http:// o https://"), 400

    name = clean_asset_name(data.get("name"), uri)
    start_date, end_date = schedule_dates(
        str(data.get("startDate", "now")),
        str(data.get("endDate", "9999-01-01T00:00:00Z")),
    )
    payload_base = {
        "name": name,
        "uri": uri,
        "start_date": start_date,
        "end_date": end_date,
        "duration": max(0, int(data.get("duration", 0))),
        "mimetype": "webpage",
        "is_enabled": bool(data.get("enabled", True)),
        "skip_asset_check": True,
    }
    def create_on_host(host):
        auth = auth_for_host(host, data)
        detected = detect_api(host, auth, api_preference_for_host(host, preferred))
        if not detected["ok"]:
            return {"host": host, "ok": False, "error": detected["error"]}
        duplicate = find_duplicate_asset(detected.get("assets", []), name, uri)
        if duplicate and duplicate_policy == "skip":
            return {
                "host": host, "ok": True, "version": detected["version"],
                "duplicate": True, "asset": duplicate,
                "message": "Ya existia un contenido equivalente",
            }
        version = detected["version"]
        payload = dict(payload_base)
        if version == "v2":
            payload.update({"is_processing": False, "nocache": False, "play_order": 0})
        else:
            payload["is_enabled"] = int(payload["is_enabled"])
            payload["skip_asset_check"] = 1
        path = "/api/v2/assets" if version == "v2" else "/api/v1.2/assets"
        created = screenly_request("POST", host, path, auth, json=payload)
        return {
            "host": host,
            "ok": created["ok"],
            "version": version,
            "asset": created.get("data"),
            "error": None if created["ok"] else created["error"],
        }
    results = parallel_map(hosts, create_on_host)
    return jsonify(results=results)


@app.post("/api/asset")
def asset_action():
    data = request.get_json(silent=True) or {}
    operation = data.get("operation", "")
    require_role("admin" if operation == "delete" else "operator")
    preferred = data.get("apiVersion", "auto")
    targets = clean_targets(data)
    if operation not in {"enable", "disable", "delete", "update"}:
        return jsonify(error="Operacion no valida"), 400
    update = data.get("update") if isinstance(data.get("update"), dict) else {}

    def apply_to_target(target):
        host = target["host"]
        asset_id = target["assetId"]
        auth = auth_for_host(host, data)
        detected = detect_api(host, auth, api_preference_for_host(host, preferred))
        if not detected["ok"]:
            return {"host": host, "assetId": asset_id, "ok": False, "error": detected["error"]}
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
        return {
            "host": host,
            "assetId": asset_id,
            "ok": response["ok"],
            "version": version,
            "error": None if response["ok"] else response["error"],
        }
    results = parallel_targets(targets, apply_to_target)
    append_history(f"asset_{operation}", None, f"{len(targets)} contenidos solicitados")
    return jsonify(results=results)


@app.post("/api/download")
def download_asset():
    data = request.get_json(silent=True) or {}
    targets = clean_targets(data)
    if len(targets) != 1:
        return jsonify(error="Selecciona un unico contenido para descargar"), 400
    target = targets[0]
    auth = auth_for_host(target["host"], data)
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
    require_role("operator")
    data = request.get_json(silent=True) or {}
    hosts = clean_hosts(data.get("hosts"))
    if len(hosts) != 1:
        return jsonify(error="El control de reproduccion requiere una sola pantalla"), 400
    direction = str(data.get("direction", ""))
    if direction not in {"previous", "next"}:
        return jsonify(error="Control de reproduccion no valido"), 400
    auth = auth_for_host(hosts[0], data)
    detected = detect_api(hosts[0], auth, api_preference_for_host(hosts[0], data.get("apiVersion", "auto")))
    if not detected["ok"]:
        return jsonify(error=detected["error"]), detected.get("status") or 502
    version = detected["version"]
    path = f"/api/v2/assets/control/{direction}" if version == "v2" else f"/api/v1/assets/control/{direction}"
    response = screenly_request("GET", hosts[0], path, auth)
    if not response["ok"]:
        return jsonify(error=response["error"]), response.get("status") or 502
    return jsonify(ok=True, host=hosts[0], direction=direction)


@app.post("/api/order")
def update_playlist_order():
    require_role("operator")
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

    auth = auth_for_host(hosts[0], data)
    detected = detect_api(hosts[0], auth, api_preference_for_host(hosts[0], data.get("apiVersion", "auto")))
    if not detected["ok"]:
        return jsonify(error=detected["error"]), detected.get("status") or 502
    known_ids = {
        str(asset.get("asset_id", asset.get("assetId", asset.get("id", ""))))
        for asset in detected.get("assets", []) if isinstance(asset, dict)
    }
    unknown = [asset_id for asset_id in ordered_ids if asset_id not in known_ids]
    if unknown:
        return jsonify(error=f"Contenido no encontrado: {unknown[0]}"), 409
    path = "/api/v2/assets/order" if detected["version"] == "v2" else "/api/v1/assets/order"
    response = screenly_request(
        "POST", hosts[0], path, auth,
        data={"ids": ",".join(ordered_ids)},
    )
    if not response["ok"]:
        return jsonify(error=response["error"]), response.get("status") or 502
    return jsonify(ok=True, host=hosts[0], orderedIds=ordered_ids)


@app.errorhandler(ValueError)
def handle_value_error(error):
    return jsonify(error=str(error)), 400


@app.errorhandler(HTTPException)
def handle_http_error(error):
    return jsonify(error=error.description), error.code


@app.errorhandler(Exception)
def handle_error(error):
    app.logger.exception(error)
    return jsonify(error="Error interno del panel. Revisa la ventana de Centro de mando Screenly."), 500


def parallel_map(items, worker, max_workers=MAX_FLEET_WORKERS):
    if not items:
        return []
    workers = max(1, min(max_workers, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(worker, items))


def parallel_map_timeout(items, worker, timeout, on_timeout, on_error=None, max_workers=MAX_FLEET_WORKERS):
    if not items:
        return []
    workers = max(1, min(max_workers, len(items)))
    results = [None] * len(items)
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = {executor.submit(worker, item): (index, item) for index, item in enumerate(items)}
    try:
        try:
            for future in as_completed(futures, timeout=timeout):
                index, item = futures[future]
                try:
                    results[index] = future.result()
                except Exception as error:
                    results[index] = on_error(item, error) if on_error else on_timeout(item)
        except FuturesTimeout:
            pass

        for future, (index, item) in futures.items():
            if results[index] is not None:
                continue
            future.cancel()
            results[index] = on_timeout(item)
        return results
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def parallel_targets(targets, worker):
    """Run different screens concurrently while preserving order per screen."""
    groups = {}
    for index, target in enumerate(targets):
        groups.setdefault(target["host"], []).append((index, target))

    def run_group(group):
        return [(index, worker(target)) for index, target in group]

    grouped_results = parallel_map(list(groups.values()), run_group)
    indexed = [item for group in grouped_results for item in group]
    return [result for _index, result in sorted(indexed, key=lambda item: item[0])]


def upload_to_host(
    host, temporary_path, filename, mimetype, name, start_date, end_date,
    duration, enabled, skip_check, duplicate_policy, auth, preferred,
):
    detected = detect_api(host, auth, preferred)
    if not detected["ok"]:
        return {"host": host, "ok": False, "error": detected["error"]}

    version = detected["version"]
    duplicate = find_duplicate_asset(detected.get("assets", []), name, filename)
    if duplicate and duplicate_policy == "skip":
        return {
            "host": host,
            "ok": True,
            "version": version,
            "duplicate": True,
            "asset": duplicate,
            "message": "Ya existia un contenido equivalente",
        }
    upload_path = "/api/v2/file_asset" if version == "v2" else "/api/v1/file_asset"
    with open(temporary_path, "rb") as content:
        uploaded = screenly_request_retry(
            "POST", host, upload_path, auth,
            files={"file_upload": (filename, content, mimetype)},
            timeout=600,
        )
    if not uploaded["ok"]:
        return result_error(host, version, uploaded)

    upload_metadata = normalize_uploaded_file(uploaded.get("data"), filename, mimetype)
    if not upload_metadata["uri"]:
        return {
            "host": host,
            "ok": False,
            "version": version,
            "error": "Screenly recibio el archivo pero no devolvio su ruta",
        }

    payload = {
        "name": name,
        "uri": upload_metadata["uri"],
        "start_date": start_date,
        "end_date": end_date,
        "duration": duration,
        "mimetype": upload_metadata["mimetype"],
        "is_enabled": enabled if version == "v2" else int(enabled),
        "skip_asset_check": skip_check if version == "v2" else int(skip_check),
    }
    if version == "v2":
        payload.update({
            "ext": upload_metadata["ext"],
            "is_processing": True,
            "nocache": False,
            "play_order": 0,
        })
    asset_path = "/api/v2/assets" if version == "v2" else "/api/v1.2/assets"
    created = screenly_request("POST", host, asset_path, auth, json=payload)
    if created["ok"]:
        append_history("asset_uploaded", host, f"Contenido subido: {name}")
    return {
        "host": host,
        "ok": created["ok"],
        "version": version,
        "asset": created.get("data"),
        "error": None if created["ok"] else created["error"],
    }


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
        fleet.append(clean_host_config(item, host))
    return fleet


def public_host(item):
    auth = item.get("auth") if isinstance(item.get("auth"), dict) else {}
    return {
        "host": item["host"],
        "name": item["name"],
        "maintenance": bool(item.get("maintenance")),
        "notes": item.get("notes", ""),
        "auth": {
            "enabled": bool(auth.get("enabled")),
            "username": str(auth.get("username", "")),
            "hasPassword": bool(auth.get("password")),
            "apiVersion": auth.get("apiVersion", "auto"),
        },
    }


def save_fleet(fleet):
    temporary = CONFIG_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(fleet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, CONFIG_PATH)


def clean_host_config(item, host=None):
    host = validate_private_host(host or item.get("host"))
    return {
        "host": host,
        "name": clean_host_name(item.get("name"), host),
        "maintenance": bool(item.get("maintenance")),
        "notes": clean_notes(item.get("notes")),
        "auth": clean_auth_config(item.get("auth") if isinstance(item.get("auth"), dict) else None),
    }


def load_settings():
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"defaultAuth": clean_auth_config(data.get("defaultAuth") if isinstance(data.get("defaultAuth"), dict) else None)}


def save_settings(settings):
    temporary = SETTINGS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, SETTINGS_PATH)


def public_settings(settings):
    auth = settings.get("defaultAuth") if isinstance(settings.get("defaultAuth"), dict) else {}
    return {
        "defaultAuth": {
            "enabled": bool(auth.get("enabled")),
            "username": str(auth.get("username", "")),
            "hasPassword": bool(auth.get("password")),
            "apiVersion": auth.get("apiVersion", "auto"),
        }
    }


def clean_auth_config(value, existing=None):
    value = value if isinstance(value, dict) else {}
    existing = existing if isinstance(existing, dict) else {}
    enabled = bool(value.get("enabled", existing.get("enabled", False)))
    username = " ".join(str(value.get("username", existing.get("username", ""))).strip().split())[:80]
    password = str(existing.get("password", ""))
    if "password" in value:
        password = str(value.get("password") or "")
    if value.get("clearPassword"):
        password = ""
    api_version = str(value.get("apiVersion", existing.get("apiVersion", "auto")))
    if api_version not in {"auto", "v1.2", "v2"}:
        api_version = "auto"
    if not enabled:
        username = ""
        password = ""
    return {"enabled": enabled, "username": username, "password": password, "apiVersion": api_version}


def clean_notes(value):
    return " ".join(str(value or "").strip().split())[:240]


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


def load_host_config(host):
    for item in load_fleet():
        if item["host"] == host:
            return item
    return {"host": host, "name": host, "maintenance": False, "auth": {}}


def clean_asset_name(value, fallback="Contenido"):
    name = " ".join(str(value or "").strip().split())
    fallback_name = " ".join(str(fallback or "Contenido").strip().split())
    return (name or fallback_name or "Contenido")[:MAX_ASSET_NAME_LENGTH]


def schedule_dates(start_value, end_value):
    start_date = iso_date(str(start_value or "now"))
    end_date = iso_date(str(end_value or "9999-01-01T00:00:00Z"))
    start = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    if end < start:
        raise ValueError("La fecha de fin debe ser posterior a la fecha de inicio")
    return start_date, end_date


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
    if not changes:
        raise ValueError("No hay cambios validos para guardar")
    if "name" in changes:
        changes["name"] = clean_asset_name(changes["name"])
    if "start_date" in changes and "end_date" in changes:
        changes["start_date"], changes["end_date"] = schedule_dates(
            changes["start_date"], changes["end_date"]
        )
    else:
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
    if username.lower() in {"undefined", "null", "none"}:
        username = ""
    if password.lower() in {"undefined", "null", "none"}:
        password = ""
    return (username, password) if username and password else None


def auth_for_host(host, data):
    manual = auth_from(data)
    if manual:
        return manual
    config = load_host_config(host).get("auth", {})
    if config.get("enabled") and config.get("username"):
        return (config["username"], str(config.get("password", "")))
    default_auth = load_settings().get("defaultAuth", {})
    if default_auth.get("enabled") and default_auth.get("username"):
        return (default_auth["username"], str(default_auth.get("password", "")))
    return None


def api_preference_for_host(host, preferred):
    if preferred in {"v1.2", "v2"}:
        return preferred
    config = load_host_config(host).get("auth", {})
    value = config.get("apiVersion")
    if value in {"v1.2", "v2"}:
        return value
    default_auth = load_settings().get("defaultAuth", {})
    value = default_auth.get("apiVersion")
    return value if value in {"v1.2", "v2"} else "auto"


def append_history(kind, host, message, detail=None):
    event = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": kind,
        "host": host,
        "message": str(message)[:300],
    }
    if detail:
        event["detail"] = str(detail)[:500]
    line = json.dumps(event, ensure_ascii=False)
    with HISTORY_LOCK:
        with HISTORY_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def load_history(limit=MAX_HISTORY_ITEMS):
    try:
        lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines[-limit:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return list(reversed(events))


def diagnose_host(host, auth, preferred):
    config = load_host_config(host)
    monitor = monitor_status(host)
    api_preferred = api_preference_for_host(host, preferred)
    detected = detect_api(host, auth, api_preferred)
    if detected["ok"]:
        active = [asset for asset in detected["assets"] if asset_status_server(asset) == "active"]
        return {
            "host": host,
            "ok": True,
            "status": "online",
            "severity": "ok" if not config.get("maintenance") else "maintenance",
            "message": "API disponible",
            "version": detected["version"],
            "assets": len(detected["assets"]),
            "active": len(active),
            "maintenance": bool(config.get("maintenance")),
            "monitor": monitor_summary(monitor),
        }
    status = detected.get("status", 0)
    if config.get("maintenance"):
        severity = "maintenance"
        label = "mantenimiento"
        message = "Pantalla marcada en mantenimiento"
    elif status in {401, 403}:
        severity = "warning"
        label = "api_protegida"
        message = "API protegida: configura usuario y contrasena de Screenly"
    elif status == 0:
        severity = "error"
        label = "sin_red"
        message = "No responde por red desde el servidor"
    else:
        severity = "warning"
        label = "api_error"
        message = detected.get("error") or "Screenly responde con error"
    return {
        "host": host,
        "ok": False,
        "status": label,
        "severity": severity,
        "message": message,
        "error": detected.get("error"),
        "httpStatus": status,
        "maintenance": bool(config.get("maintenance")),
        "monitor": monitor_summary(monitor),
    }


def asset_status_server(asset):
    enabled = asset.get("is_enabled") in {True, 1, "1"}
    if not enabled:
        return "inactive"
    now = datetime.now(timezone.utc)
    try:
        start = datetime.fromisoformat(str(asset.get("start_date", "")).replace("Z", "+00:00"))
    except ValueError:
        start = None
    try:
        end = datetime.fromisoformat(str(asset.get("end_date", "")).replace("Z", "+00:00"))
    except ValueError:
        end = None
    if end and end.year < 9999 and end < now:
        return "inactive"
    if start and start > now:
        return "scheduled"
    return "active"


def find_duplicate_asset(assets, name, marker):
    wanted_name = str(name or "").strip().lower()
    wanted_marker = Path(str(marker or "")).name.lower()
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        asset_name = str(asset.get("name") or asset.get("title") or "").strip().lower()
        asset_uri = Path(str(asset.get("uri") or "")).name.lower()
        if wanted_name and asset_name == wanted_name:
            return asset
        if wanted_marker and asset_uri and wanted_marker == asset_uri:
            return asset
    return None


def load_monitor_config():
    try:
        data = json.loads(MONITOR_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"token": "", "tokens": {}, "port": 8765}
    raw_tokens = data.get("hosts") if isinstance(data.get("hosts"), dict) else {}
    tokens = {}
    for host, value in raw_tokens.items():
        token = value.get("token") if isinstance(value, dict) else value
        if isinstance(token, str) and token.strip():
            tokens[str(host)] = token.strip()
    return {
        "token": str(data.get("token", "")),
        "tokens": tokens,
        "port": max(1, min(65535, int(data.get("port", 8765)))),
    }


def monitor_credentials(host):
    config = load_monitor_config()
    return {
        "token": config["tokens"].get(host) or config["token"],
        "port": config["port"],
    }


def monitor_status(host):
    config = monitor_credentials(host)
    if not config["token"]:
        return {"ok": False, "connected": False, "reason": "unconfigured"}
    try:
        response = requests.get(
            f"http://{host}:{config['port']}/status",
            headers={"X-Fleet-Token": config["token"]}, timeout=(1, 1.5),
        )
        if response.status_code in {401, 403}:
            return {"ok": False, "connected": True, "reason": "unauthorized"}
        data = response.json() if response.ok else {}
        if isinstance(data, dict):
            data["connected"] = True
            return data
        return {"ok": False, "connected": True, "reason": "invalid_response"}
    except (requests.RequestException, ValueError):
        return {"ok": False, "connected": False, "reason": "unreachable"}


def monitor_summary(telemetry):
    return {
        "connected": bool(telemetry.get("connected")),
        "version": telemetry.get("agentVersion"),
        "reason": telemetry.get("reason"),
    }


def screenly_request(method, host, path, auth, timeout=120, connect_timeout=2, **kwargs):
    try:
        response = requests.request(
            method, f"http://{host}{path}", auth=auth,
            timeout=(connect_timeout, timeout), headers={"Accept": "application/json"}, **kwargs,
        )
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text}
        if not response.ok:
            message = response_error_message(data, response.status_code)
            return {"ok": False, "status": response.status_code, "error": message, "data": data}
        return {"ok": True, "status": response.status_code, "data": data}
    except requests.RequestException as error:
        return {"ok": False, "status": 0, "error": network_error_message(error)}


def screenly_request_retry(method, host, path, auth, timeout=120, connect_timeout=4, retries=UPLOAD_RETRIES, **kwargs):
    last_response = None
    for attempt in range(retries + 1):
        rewind_upload_files(kwargs)
        response = screenly_request(method, host, path, auth, timeout=timeout, connect_timeout=connect_timeout, **kwargs)
        last_response = response
        if response["ok"] or response.get("status"):
            return response
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    return last_response or {"ok": False, "status": 0, "error": "Error de red al comunicarse con Screenly"}


def sniff_media_type(chunk):
    if not chunk:
        return None
    if chunk.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if chunk.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if chunk.startswith(b"GIF87a") or chunk.startswith(b"GIF89a"):
        return "image/gif"
    if chunk.startswith(b"RIFF") and chunk[8:12] == b"WEBP":
        return "image/webp"
    if len(chunk) > 12 and chunk[4:8] == b"ftyp":
        return "video/mp4"
    return None


def rewind_upload_files(kwargs):
    files = kwargs.get("files")
    if not isinstance(files, dict):
        return
    for value in files.values():
        stream = None
        if hasattr(value, "seek"):
            stream = value
        elif isinstance(value, (tuple, list)) and len(value) >= 2 and hasattr(value[1], "seek"):
            stream = value[1]
        if stream:
            try:
                stream.seek(0)
            except OSError:
                pass


def response_error_message(data, status):
    message = ""
    if isinstance(data, dict):
        message = data.get("error") or data.get("message") or data.get("detail") or data.get("raw") or ""
    elif isinstance(data, str):
        message = data
    if isinstance(message, (dict, list)):
        message = json.dumps(message, ensure_ascii=False)
    message = re.sub(r"<[^>]+>", " ", str(message))
    message = " ".join(message.split())[:300]
    if status in {401, 403}:
        return "Screenly requiere usuario y contrasena"
    if status == 404:
        return "La funcion solicitada no existe en esta version de Screenly"
    return message or f"Screenly respondio con el estado HTTP {status}"


def network_error_message(error):
    if isinstance(error, requests.ConnectTimeout):
        return "La Raspberry tarda demasiado en aceptar la conexion"
    if isinstance(error, requests.ReadTimeout):
        return "Screenly tarda demasiado en responder"
    if isinstance(error, requests.ConnectionError):
        return "No se puede conectar con la Raspberry"
    return "Error de red al comunicarse con Screenly"


def detect_api(host, auth, preferred="auto"):
    versions = [preferred] if preferred in {"v2", "v1.2"} else ["v2", "v1.2"]
    last_error = "No responde la API de Screenly/Anthias"
    last_status = 0
    for version in versions:
        path = "/api/v2/assets" if version == "v2" else "/api/v1.2/assets"
        response = screenly_request("GET", host, path, auth, timeout=6, connect_timeout=1.5)
        last_status = response.get("status", 0)
        if response["ok"]:
            assets = normalize_assets(response.get("data"))
            if assets is not None:
                return {"ok": True, "version": version, "assets": assets, "status": last_status}
            last_error = f"La API {version} no devolvio una lista de contenidos"
            continue
        last_error = response.get("error", last_error)
        if last_status == 0:
            break
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


def normalize_uploaded_file(data, filename, browser_mimetype=""):
    """Normalize upload responses from old Screenly OSE and newer Anthias."""
    content_kind = "image" if str(browser_mimetype).startswith("image/") else "video"
    default_ext = Path(filename).suffix
    if isinstance(data, str):
        return {"uri": data.strip(), "mimetype": content_kind, "ext": default_ext}
    if isinstance(data, dict):
        return {
            "uri": str(data.get("uri", "")).strip(),
            "mimetype": data.get("mimetype") or content_kind,
            "ext": data.get("ext") or default_ext,
        }
    return {"uri": "", "mimetype": content_kind, "ext": default_ext}


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
    print(f"Centro de mando Screenly disponible en {url}")
    print("Para cerrarlo, cierra esta ventana o pulsa Ctrl+C.")
    app.run(host="127.0.0.1", port=5000, debug=False)
    duplicate_policy = str(data.get("duplicatePolicy", "skip"))
