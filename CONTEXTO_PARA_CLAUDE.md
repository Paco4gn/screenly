# Contexto completo para continuar con Claude

Este documento resume la conversacion, las decisiones tomadas y el estado real del
proyecto Fleetboard. Esta pensado para entregar el repositorio a otro asistente y
que pueda continuar sin volver a investigar todo desde cero.

> Seguridad: no se incluye ninguna contrasena, token ni contenido de `fleet.json`
> o `monitor.json`. Las credenciales SSH solo deben introducirse durante la
> instalacion y nunca deben subirse a GitHub.

## Objetivo del usuario

Administrar desde un unico ordenador varias Raspberry Pi con Screenly OSE, sin
actualizar ni modificar la version de Screenly instalada. El panel debe funcionar
en Flask, abrirse en el navegador y evitar que el usuario tenga que entrar una por
una en cada interfaz de Screenly.

Las Raspberry iniciales usan estas IP:

- `192.168.20.223`
- `192.168.20.224`
- `192.168.20.225`
- `192.168.20.226`
- `192.168.20.227`
- `192.168.20.228`

## Conversacion cronologica

1. El usuario explico que tenia varias Raspberry con Screenly OSE y que subir los
   videos individualmente era muy lento. Facilito el rango de IP anterior.
2. Aclaro que no se debia tocar ni actualizar la version instalada de Screenly.
   Queria una herramienta externa para administrarlas todas a la vez.
3. Pregunto como utilizarla desde su ordenador y pidio expresamente una interfaz
   web con Flask, sin tener que instalar una aplicacion de escritorio.
4. Facilito capturas de la interfaz antigua de Screenly OSE. Solicito una interfaz
   mas atractiva y completa, conservando las funciones normales de Screenly:
   biblioteca, contenidos activos e inactivos, programacion, activacion,
   desactivacion, edicion, descarga, eliminacion y orden de reproduccion.
5. Al principio el panel no encontraba contenidos aunque Screenly si tenia assets.
   Se adapto la consulta a la API de esta version de Screenly y se mostraron los
   contenidos de cada reproductor.
6. Solicito poder administrar una Raspberry de forma individual, ademas del modo
   conjunto, y poder descargar videos e imagenes.
7. Solicito alta, renombrado y baja de Raspberry desde la propia interfaz. La lista
   paso a ser configurable y se guarda localmente en `fleet.json`.
8. Detecto que al entrar a administrar una pantalla no podia volver a seleccionar
   varias a la vez. Se corrigio la seleccion para permitir alternar entre modo
   individual y seleccion multiple desde la barra lateral.
9. Solicito saber que contenido se estaba visualizando en cada pantalla y ver el
   video o imagen en tiempo real.
10. Una primera vista solo indicaba el nombre del recurso y ofrecia reproducir el
    archivo desde el principio. El usuario aclaro que queria conocer el punto real
    de reproduccion, no una reproduccion independiente del navegador.
11. Se explico que Flask y la API de Screenly no entregan por si solos una captura
    HDMI. Para una replica sincronizada hace falta un agente de solo lectura en
    cada Raspberry o una capturadora HDMI fisica.
12. El usuario eligio la opcion del agente instalado en cada Raspberry y pidio que
    todas las pantallas se vieran simultaneamente dentro de Fleetboard.
13. Confirmo que el acceso SSH usa el usuario `pi`. La contrasena fue comunicada en
    la conversacion original, pero se ha omitido deliberadamente de este documento
    y del repositorio.
14. Se creo `fleet_monitor_agent.py`, un instalador desde Windows y documentacion
    detallada para instalar el agente en el resto de Raspberry.
15. El usuario pidio subir el proyecto a `Paco4gn/screenly`. Tras instalar y
    autenticar GitHub CLI, se creo la rama `codex/fleetboard-screenly`, se hizo el
    commit inicial y se abrio el pull request numero 1.

## Que hace actualmente Fleetboard

- Consulta varias instalaciones Screenly OSE desde un unico panel.
- Permite seleccionar una, varias o todas las pantallas.
- Muestra assets activos, inactivos y programados.
- Sube archivos de video o imagen a varias pantallas.
- Crea contenidos mediante URL.
- Activa, desactiva, edita y elimina assets individualmente o en lote.
- Descarga el contenido de un asset.
- Filtra, busca y ordena la biblioteca.
- Cambia el orden de la playlist.
- Ejecuta anterior y siguiente en modo individual.
- Anade, renombra y elimina Raspberry de la configuracion local.
- Muestra el contenido actual de varias Raspberry simultaneamente.
- Para videos, reconstruye la reproduccion en el navegador y sincroniza la
  posicion consultada al reproductor aproximadamente cada segundo.
- Para imagenes, muestra el recurso real que Screenly esta presentando.

## Arquitectura

### Panel de Windows

- `app.py`: servidor Flask, proxy de la API de Screenly y proxy del agente.
- `index.html`, `styles.css`, `app.js`: interfaz Fleetboard.
- `INICIAR_PANEL.bat`: instala dependencias en `vendor` si faltan e inicia Flask.
- `fleet.json`: lista local de pantallas. Esta ignorado por Git.
- `monitor.json`: tokens del agente por IP. Esta ignorado por Git.

El panel se abre en `http://127.0.0.1:5000` y solo escucha en el ordenador local.

### Agente de Raspberry

- `fleet_monitor_agent.py`: servicio HTTP de solo lectura en el puerto `8765`.
- Lee la posicion real de OMXPlayer mediante DBus.
- Sirve al panel el mismo archivo que Screenly esta reproduciendo.
- No cambia Screenly, la playlist, los assets ni el estado de reproduccion.
- Se instala como `fleet-monitor-agent.service` y arranca con systemd.
- Usa un token local distinto por Raspberry.

La vista del navegador es una reconstruccion sincronizada del recurso. No es una
captura electrica exacta de la salida HDMI. Para verificar pixeles exactos,
overlays o fallos fisicos del HDMI harian falta capturadoras HDMI.

## Instalacion del agente

Desde el ordenador:

1. Abrir `INSTALAR_AGENTE.bat`.
2. Introducir la IP de la Raspberry.
3. Introducir el nombre que aparecera en Fleetboard.
4. Usar el usuario SSH `pi`.
5. Introducir la contrasena SSH cuando la solicite el instalador.
6. Esperar al mensaje `LISTO`.
7. Actualizar Fleetboard y abrir `En pantalla`.

Archivos instalados en cada Raspberry:

- `/home/pi/fleet_monitor_agent.py`
- `/home/pi/.fleet-monitor-token`
- `/etc/systemd/system/fleet-monitor-agent.service`

Comprobacion manual:

```bash
systemctl status fleet-monitor-agent.service
systemctl status screenly-viewer.service
journalctl -u fleet-monitor-agent.service -n 50 --no-pager
```

Las instrucciones completas y la desinstalacion estan en
`INSTALAR_AGENTE.md`.

## Restricciones y decisiones importantes

- No actualizar Screenly OSE ni instalar una version diferente.
- No cambiar la playlist ni la configuracion interna desde el agente.
- No guardar la contrasena SSH.
- No subir `fleet.json`, `monitor.json`, `vendor`, `.env` ni caches a GitHub.
- Aceptar solamente IPv4 privadas para nuevas pantallas.
- Mantener el panel sencillo de abrir desde Windows mediante archivos `.bat`.
- La API cambia entre versiones antiguas de Screenly; conservar la deteccion y
  normalizacion existente en `app.py`.
- La indicacion de que una pantalla requiere clave no debe aparecer simplemente
  por estar desconectada. Diferenciar autenticacion, red y agente no instalado.

## Problemas que conviene revisar a continuacion

1. Probar la instalacion del agente en las seis Raspberry reales y documentar las
   variantes si alguna no usa OMXPlayer o tiene otro nombre de servicio.
2. Verificar durante varias horas que la sincronizacion no deriva en videos largos.
3. Mejorar el diagnostico visual entre estos estados: Screenly sin conexion,
   agente sin instalar, token incorrecto, asset no disponible y reproductor parado.
4. Evitar descargar el mismo video repetidamente cuando varias tarjetas muestran
   un recurso identico; una cache local temporal reduciria trafico de red.
5. Incorporar un limite de concurrencia para despliegues grandes, manteniendo el
   comportamiento actual para las seis pantallas.
6. Anadir pruebas automatizadas de las normalizaciones de API y de las operaciones
   masivas antes de ampliar el alcance.
7. Revisar accesibilidad, estados de carga, mensajes de error y vista movil con
   capturas reales del navegador.

## Estado de GitHub

- Repositorio: `https://github.com/Paco4gn/screenly`
- Rama de trabajo: `codex/fleetboard-screenly`
- Pull request: `https://github.com/Paco4gn/screenly/pull/1`
- Rama base: `main`
- El pull request se creo como borrador.

## Instruccion sugerida para Claude

Puedes comenzar con este mensaje:

> Lee por completo `CONTEXTO_PARA_CLAUDE.md`, `README.md` e
> `INSTALAR_AGENTE.md`. Despues revisa el codigo sin cambiar la version de
> Screenly de ninguna Raspberry. Conserva la compatibilidad con la API antigua,
> no expongas credenciales y trabaja sobre la rama existente. Antes de modificar
> nada, explica brevemente que parte vas a mejorar y valida el resultado en
> `http://127.0.0.1:5000`.
