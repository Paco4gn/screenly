import getpass
import ipaddress
import json
import secrets
import sys
import time
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = BASE_DIR / "vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

import paramiko
import requests


DEFAULT_PANEL_URL = "http://172.31.139.45"
MONITOR_CONFIG = BASE_DIR / "monitor.json"
FLEET_CONFIG = BASE_DIR / "fleet.json"
AGENT_FILE = BASE_DIR / "fleet_monitor_agent.py"
SERVICE = """[Unit]
Description=Centro de mando Screenly - monitor de reproduccion
After=network-online.target screenly-viewer.service
Wants=network-online.target

[Service]
Type=simple
User=pi
ExecStart=/usr/bin/python3 /home/pi/fleet_monitor_agent.py
Restart=always
RestartSec=2
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=multi-user.target
"""
DEFAULT_FLEET = [
    {"host": "192.168.20.{}".format(number), "name": "Pantalla {}".format(index)}
    for index, number in enumerate(range(223, 229), start=1)
]


def private_ip(value):
    address = ipaddress.ip_address(value.strip())
    if address.version != 4 or not address.is_private or address.is_loopback:
        raise ValueError("La IP debe ser una direccion IPv4 privada de la red local")
    return str(address)


def write_json_atomic(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_or_create_token(host):
    try:
        data = json.loads(MONITOR_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    hosts = data.get("hosts") if isinstance(data.get("hosts"), dict) else {}
    current = hosts.get(host)
    current_token = current.get("token") if isinstance(current, dict) else current
    token = str(current_token or "").strip() or secrets.token_urlsafe(32)
    hosts[host] = {"token": token}
    data["hosts"] = hosts
    data["port"] = 8765
    write_json_atomic(MONITOR_CONFIG, data)
    return token


def clean_panel_url(value):
    url = str(value or "").strip().rstrip("/")
    if not url:
        raise ValueError("La URL del panel no puede estar vacia")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


def login_panel(panel_url, email, password):
    session = requests.Session()
    response = session.post(
        panel_url + "/api/login",
        json={"email": email, "password": password},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError("El panel no acepto el inicio de sesion")
    return session


def request_panel_token(session, panel_url, host):
    response = session.post(panel_url + "/api/monitor-token", json={"host": host}, timeout=10)
    response.raise_for_status()
    payload = response.json()
    token = str(payload.get("token", "")).strip()
    if not token:
        raise RuntimeError("El panel no devolvio token para el agente")
    return token


def deploy(host, username, password, token):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host, username=username, password=password, timeout=10,
        look_for_keys=False, allow_agent=False, auth_timeout=10, banner_timeout=10,
    )
    try:
        sftp = client.open_sftp()
        files = (
            ("/home/pi/fleet_monitor_agent.py", AGENT_FILE.read_text(encoding="utf-8")),
            ("/home/pi/.fleet-monitor-token", token + "\n"),
            ("/home/pi/fleet-monitor-agent.service", SERVICE),
        )
        for remote_path, content in files:
            with sftp.file(remote_path, "w") as remote:
                remote.write(content)
        sftp.chmod("/home/pi/fleet_monitor_agent.py", 0o755)
        sftp.chmod("/home/pi/.fleet-monitor-token", 0o600)
        sftp.close()

        command = (
            "sudo -S -p '' install -m 0644 /home/pi/fleet-monitor-agent.service "
            "/etc/systemd/system/fleet-monitor-agent.service && "
            "sudo systemctl daemon-reload && "
            "sudo systemctl enable fleet-monitor-agent.service && "
            "sudo systemctl restart fleet-monitor-agent.service && "
            "systemctl is-active fleet-monitor-agent.service"
        )
        stdin, stdout, stderr = client.exec_command(command, timeout=30)
        stdin.write(password + "\n")
        stdin.flush()
        output = stdout.read().decode("utf-8", "replace").strip()
        error = stderr.read().decode("utf-8", "replace").strip()
        if "active" not in output:
            raise RuntimeError(error or output or "El servicio no pudo iniciarse")
    finally:
        client.close()


def verify(host, token):
    last_error = None
    for _attempt in range(8):
        try:
            response = requests.get(
                "http://{}:8765/status".format(host),
                headers={"X-Fleet-Token": token}, timeout=5,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            last_error = error
            time.sleep(1)
    raise RuntimeError("El agente no responde: {}".format(last_error))


def add_to_panel(host, name, panel_url=None, session=None):
    if panel_url and session:
        response = session.get(panel_url + "/api/hosts", timeout=10)
        response.raise_for_status()
        hosts = response.json().get("hosts", [])
        existing = next((item for item in hosts if item.get("host") == host), None)
        if existing:
            session.patch(
                panel_url + "/api/hosts/{}".format(host),
                json={"name": name}, timeout=10,
            ).raise_for_status()
        else:
            session.post(
                panel_url + "/api/hosts",
                json={"host": host, "name": name}, timeout=10,
            ).raise_for_status()
        return "Panel del servidor actualizado"

    try:
        response = requests.get("http://127.0.0.1:5000/api/hosts", timeout=2)
        response.raise_for_status()
        hosts = response.json().get("hosts", [])
        existing = next((item for item in hosts if item.get("host") == host), None)
        if existing:
            requests.patch(
                "http://127.0.0.1:5000/api/hosts/{}".format(host),
                json={"name": name}, timeout=3,
            ).raise_for_status()
        else:
            requests.post(
                "http://127.0.0.1:5000/api/hosts",
                json={"host": host, "name": name}, timeout=3,
            ).raise_for_status()
        return "Panel actualizado mediante Flask"
    except requests.RequestException:
        try:
            fleet = json.loads(FLEET_CONFIG.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fleet = [dict(item) for item in DEFAULT_FLEET]
        if not isinstance(fleet, list):
            fleet = [dict(item) for item in DEFAULT_FLEET]
        item = next((item for item in fleet if item.get("host") == host), None)
        if item:
            item["name"] = name
        else:
            fleet.append({"host": host, "name": name})
        write_json_atomic(FLEET_CONFIG, fleet)
        return "Panel actualizado en fleet.json"


def main():
    print("=" * 58)
    print(" INSTALADOR DEL AGENTE CENTRO DE MANDO SCREENLY")
    print("=" * 58)
    try:
        host = private_ip(input("IP de la Raspberry: "))
        name = input("Nombre en el panel [Pantalla {}]: ".format(host.split(".")[-1])).strip()
        name = name or "Pantalla {}".format(host.split(".")[-1])
        panel_url = clean_panel_url(
            input("URL del panel [{}]: ".format(DEFAULT_PANEL_URL)).strip() or DEFAULT_PANEL_URL
        )
        panel_email = input("Usuario del panel [informatica@feval.com]: ").strip() or "informatica@feval.com"
        panel_password = getpass.getpass("Contrasena del panel: ")
        if not panel_password:
            raise ValueError("La contrasena del panel no puede estar vacia")
        username = "pi"
        print("Usuario SSH: pi")
        password = getpass.getpass("Contrasena SSH: ")
        if not password:
            raise ValueError("La contrasena SSH no puede estar vacia")

        print("\n1/4 Conectando con el panel y obteniendo token...")
        session = login_panel(panel_url, panel_email, panel_password)
        token = request_panel_token(session, panel_url, host)
        print("2/4 Conectando y copiando el agente...")
        deploy(host, username, password, token)
        print("3/4 Servicio instalado y activo.")
        status = verify(host, token)
        print("4/4 Telemetria verificada: {}".format(status))
        print(add_to_panel(host, name, panel_url, session))
        print("\nLISTO. Actualiza Centro de mando Screenly y abre 'En pantalla'.")
        print("Las contrasenas no se han guardado en este ordenador.")
        return 0
    except Exception as error:
        print("\nERROR: {}".format(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
