# Instalar el agente Fleetboard

Este procedimiento anade el monitor de posicion y video sincronizado a otra Raspberry con Screenly OSE. No actualiza Screenly ni cambia su playlist.

## Requisitos

- El ordenador y la Raspberry deben estar en la misma red.
- La Raspberry debe estar encendida y tener SSH habilitado.
- Debes conocer la contrasena SSH del usuario `pi`.
- Screenly debe estar reproduciendo mediante OMXPlayer.

## Instalacion automatica

1. Abre la carpeta `screenly-fleet`.
2. Haz doble clic en `INSTALAR_AGENTE.bat`.
3. Escribe la IP completa, por ejemplo `192.168.20.229`.
4. Escribe el nombre que aparecera en Fleetboard.
5. El instalador utilizara el usuario SSH `pi`.
6. Escribe la contrasena SSH. Mientras escribes no aparecen caracteres; es normal.
7. Espera hasta ver el mensaje `LISTO`.
8. Abre o actualiza `http://127.0.0.1:5000`.
9. Entra en `En pantalla`.

El instalador se puede ejecutar de nuevo sobre la misma IP para actualizar o reparar el agente. No crea pantallas duplicadas.

## Archivos instalados en la Raspberry

- `/home/pi/fleet_monitor_agent.py`: agente de lectura.
- `/home/pi/.fleet-monitor-token`: token de comunicacion, con permisos `600`.
- `/etc/systemd/system/fleet-monitor-agent.service`: servicio de arranque automatico.

El servicio escucha en el puerto TCP `8765` de la red local. La contrasena SSH solo se utiliza durante la instalacion y no se guarda.

## Comprobarlo manualmente

Desde una terminal SSH de la Raspberry:

```bash
systemctl status fleet-monitor-agent.service
systemctl status screenly-viewer.service
```

Los dos deben mostrar `active (running)`.

Para ver los ultimos mensajes del servicio:

```bash
journalctl -u fleet-monitor-agent.service -n 50 --no-pager
```

## Desinstalar el agente

Conecta por SSH y ejecuta:

```bash
sudo systemctl disable --now fleet-monitor-agent.service
sudo rm /etc/systemd/system/fleet-monitor-agent.service
sudo systemctl daemon-reload
rm /home/pi/fleet_monitor_agent.py
rm /home/pi/.fleet-monitor-token
```

Despues elimina la pantalla desde el boton `+` situado junto a `Destino` en Fleetboard. Esto no elimina ni modifica Screenly.

## Problemas habituales

- `Connection timed out`: la IP no responde, la Raspberry esta apagada o SSH esta deshabilitado.
- `Authentication failed`: usuario o contrasena SSH incorrectos.
- `El servicio no pudo iniciarse`: revisa el resultado de `journalctl` indicado arriba.
- El panel muestra la pantalla pero no el video: comprueba que el puerto `8765` no este bloqueado y que OMXPlayer este reproduciendo.
- La Raspberry aparece sin conexion: pulsa `Actualizar` y verifica primero que su pagina Screenly abre desde el navegador.

Por seguridad, cambia la contrasena SSH predeterminada cuando todas las Raspberry esten configuradas. El agente seguira funcionando despues del cambio.
