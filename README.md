# Panel Screenly con Flask

Panel local para administrar las Raspberry `192.168.20.223` a `192.168.20.228` sin instalar ni actualizar nada en ellas.

## Abrir el panel

1. Conecta el ordenador a la red `192.168.20.x`.
2. Haz doble clic en `INICIAR_PANEL.bat`.
3. El navegador se abrira en `http://127.0.0.1:5000`.
4. No cierres la ventana negra mientras utilices el panel.
5. Para terminar, cierra esa ventana.

Flask y las librerias necesarias estan guardadas en la carpeta `vendor`; no se instalan programas ni paquetes en Windows. El servidor solo escucha en el propio ordenador.

En una copia nueva descargada desde GitHub, `INICIAR_PANEL.bat` prepara automaticamente esa carpeta `vendor` durante el primer arranque. Es necesario tener Python instalado y conexion a Internet solo en esa primera preparacion.

## Instalar el monitor en otra Raspberry

Haz doble clic en `INSTALAR_AGENTE.bat` y sigue las preguntas. El procedimiento completo, rutas, comprobaciones y desinstalacion estan en `INSTALAR_AGENTE.md`.

## Funciones

- Estado conjunto de todas las pantallas configuradas.
- Monitor `En pantalla` con video y posicion sincronizados aproximadamente cada segundo.
- Previsualizacion real del contenido actual cuando es una imagen.
- Flota configurable desde el boton `+` situado junto a `Destino`.
- Alta de nuevas Raspberry mediante nombre y direccion IP privada.
- Renombrado y eliminacion de pantallas sin modificar el dispositivo.
- Biblioteca con contenidos activos, inactivos y programados.
- Subida de videos o imagenes a varias pantallas.
- Creacion de contenidos mediante una direccion web.
- Busqueda y filtros por pantalla o estado.
- Ordenacion por playlist, nombre, pantalla o estado.
- Actualizacion automatica opcional cada minuto.
- Administracion individual pulsando el nombre de una pantalla.
- Descarga de videos, imagenes y enlaces desde cada fila.
- Control de contenido anterior y siguiente en modo individual.
- Cambio del orden de reproduccion cuando existen varios contenidos activos.
- Activacion, desactivacion y eliminacion individual o multiple.
- Edicion de nombre, fechas, duracion y actividad.

Cada operacion se envia mediante la API que ya incluye Screenly. El panel no instala ni actualiza software en las Raspberry.

El agente `fleet_monitor_agent.py` consulta la posicion real de OMXPlayer mediante DBus y sirve el mismo archivo al panel. La imagen mostrada es una reconstruccion sincronizada del contenido, no una captura electrica de la salida HDMI.

El agente se ejecuta como `fleet-monitor-agent.service` y arranca automaticamente con cada Raspberry. Es de solo lectura: no cambia la version, configuracion, playlist ni reproduccion de Screenly. La contrasena SSH no se guarda en el panel; la comunicacion posterior usa el token local de `monitor.json`.

La lista de pantallas se guarda en `fleet.json`. Solo se aceptan direcciones IPv4 privadas de la red local.
