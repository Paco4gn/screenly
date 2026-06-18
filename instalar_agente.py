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


MONITOR_CONFIG = BASE_DIR / "monitor.json"
FLEET_CONFIG = BASE_DIR / "fleet.json"
AGENT_FILE = BASE_DIR / "fleet_monitor_agent.py"
SERVICE = """[Unit]
Description=Fleetboard read-only playback monitor
After=network.target screenly-viewer.service

[Service]
Type=simple
User=pi
ExecStart=/usr/bin/python3 /home/pi/fleet_monitor_agent.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
"""


def private_ip(value):
    address = ipaddress.ip_address(value.strip())
    if address.version != 4 or not address.is_private or address.is_loopback:
        raise ValueError("La IP debe ser una direccion IPv4 privada de la red local")
    return str(address)


def load_or_create_token():
    try:
        data = json.loads(MONITOR_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    token = str(data.get("token", "")).strip() or secrets.token_urlsafe(32)
    MONITOR_CONFIG.write_text(
        json.dumps({"token": token, "port": 8765}, indent=2) + "\n",
        encoding="utf-8",
    )
    return token


def deploy(host, username, password, token):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host, username=username, password=password, timeout=10,
        look_for_keys=False, allow_agent=False,
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


def add_to_panel(host, name):
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
            fleet = []
        item = next((item for item in fleet if item.get("host") == host), None)
        if item:
            item["name"] = name
        else:
            fleet.append({"host": host, "name": name})
        FLEET_CONFIG.write_text(
            json.dumps(fleet, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return "Panel actualizado en fleet.json"


def main():
    print("=" * 58)
    print(" INSTALADOR DEL AGENTE FLEETBOARD PARA SCREENLY OSE")
    print("=" * 58)
    try:
        host = private_ip(input("IP de la Raspberry: "))
        name = input("Nombre en el panel [Pantalla {}]: ".format(host.split(".")[-1])).strip()
        name = name or "Pantalla {}".format(host.split(".")[-1])
        username = "pi"
        print("Usuario SSH: pi")
        password = getpass.getpass("Contrasena SSH: ")
        if not password:
            raise ValueError("La contrasena SSH no puede estar vacia")

        print("\n1/3 Conectando y copiando el agente...")
        token = load_or_create_token()
        deploy(host, username, password, token)
        print("2/3 Servicio instalado y activo.")
        status = verify(host, token)
        print("3/3 Telemetria verificada: {}".format(status))
        print(add_to_panel(host, name))
        print("\nLISTO. Actualiza Fleetboard y abre 'En pantalla'.")
        print("La contrasena SSH no se ha guardado.")
        return 0
    except Exception as error:
        print("\nERROR: {}".format(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
