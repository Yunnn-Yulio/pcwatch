# PCWatch

A small, local Windows recorder for investigating freezes, memory pressure and wake/display problems. **Initial release: 0.1.0.** Spanish user interface and documentation. Windows x64; .NET Framework; MIT license.

PCWatch guarda el estado de la PC antes de un problema para poder compararlo con los eventos del siguiente arranque. Es una herramienta inicial de diagnóstico, probada en una máquina Windows 11; no determina automáticamente qué pieza está averiada.

## Empezar

Descarga el paquete portable de [Releases](../../releases), extrae la carpeta y ejecuta `PCWatch.exe`. El icono de la bandeja permite ver el estado, abrir registros, marcar la hora de un incidente y detener el monitor.

El inicio automático es **opcional**: usa `Activar-inicio-automatico.cmd` para iniciarlo al entrar en tu cuenta. `Quitar-inicio-automatico.cmd` elimina su acceso directo de Inicio y detiene el monitor. Los registros se conservan. No se instala un servicio ni un driver.

Los logs se guardan en `%LOCALAPPDATA%\PCWatch\Logs`. Se usan fechas **UTC**. Para detenerlo, usa el menú de la bandeja o `Detener.cmd`; `Iniciar.cmd` vuelve a iniciarlo.

## Qué registra

| Cada | Datos |
|---|---|
| 15 segundos | CPU total; RAM usada y disponible; memoria comprometida/límite; pools del sistema; paginación; cola, latencia y tráfico de discos; memoria gráfica dedicada/compartida agregada; consumo del propio monitor |
| 60 segundos | Top de procesos por memoria privada y CPU, ventanas visibles que Windows identifica como colgadas y eventos técnicos seleccionados |
| Al ocurrir | Pantalla encendida/apagada/atenuada; bloqueo/desbloqueo; suspensión/reanudación; marcas manuales |
| Al iniciar | Versiones de Windows, BIOS, GPU y configuración de RAM, sin números de serie |

`resources-*.csv` contiene medidas para Excel. `processes-*.jsonl` contiene procesos, `events-*.jsonl` los eventos filtrados y `activity-*.jsonl` las transiciones y huecos de muestreo. `status.json`, en la carpeta superior, muestra el estado y la última muestra.

Los archivos rotan al alcanzar **5 MiB**, se eliminan archivos propios sin actualizar durante **14 días** y el almacenamiento de logs propios se recorta a **100 MiB** cada minuto. Durante ese intervalo puede haber un pequeño exceso. Si el límite de espacio se alcanza antes, se conserva menos historia. Cada escritura se vacía al disco; un apagado brusco aún puede perder las últimas muestras.

## Privacidad

La aplicación no tiene funciones de red ni envía datos. No registra documentos, teclas, contraseñas, portapapeles, capturas, cámara, audio, historial web, títulos de ventanas, comandos, rutas de procesos, mensajes completos de eventos ni volcados de memoria. Los nombres de procesos sí revelan qué programas estaban abiertos.

Los eventos usan una lista de campos técnicos permitidos. Para WHEA/CPER se conserva una cabecera mínima y tipos de sección; se excluyen el payload y los identificadores del registro. No cambia permisos para acceder a informes restringidos de Windows. Esta protección se prueba con datos sintéticos; los logs reales deben revisarse antes de compartirlos.

## Consumo y límites

En una prueba local de Windows 11 con 24 procesadores lógicos, el monitor usó aproximadamente **50–60 MiB de RAM** y menos de **0,1 % de CPU total** en las muestras observadas. Son mediciones de una configuración concreta, no garantías para todos los equipos. Ejecuta un único monitor por carpeta de datos y usa prioridad inferior a la normal.

- Un bloqueo completo puede detener también el monitor. Un hueco indica muestreo interrumpido, no demuestra su causa.
- La memoria gráfica usada no equivale al porcentaje de carga de GPU. No se miden temperatura ni carga de GPU.
- Un contador ausente queda vacío; los permisos y controladores pueden limitar la cobertura de eventos y procesos. Los procesos a los que no se puede acceder no forman parte del ranking.
- Memoria alta no prueba agotamiento. No sumes Working Sets para calcular RAM total: los procesos comparten páginas. Paginación no prueba por sí sola falta de RAM.
- La versión inicial no se ha validado en todas las ediciones de Windows ni frente a fallos reales provocados. No ejecuta pruebas de estrés, no cierra programas ni modifica ajustes de energía.
- Si falla el almacenamiento, el estado del monitor indica el error y la hora de la última muestra exitosa. No confíes en una muestra antigua como si fuera actual.

## Compilar desde código

En Windows x64, sin NuGet ni SDK externo:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -Package
```

La política `Bypass` se aplica solo al proceso de compilación, no cambia la configuración permanente del equipo. El ejecutable queda en `artifacts\bin\PCWatch.exe` y el ZIP portable en `artifacts`. También puedes abrir `PCWatch.csproj` con Visual Studio/MSBuild y el Developer Pack de .NET Framework 4.8.

Pruebas aisladas, sin tocar los logs operativos ni activar Inicio:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\PrivacyRetention.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\Smoke.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\Health.ps1
```

Los resultados quedan en `artifacts`, excluido de Git. El workflow de Windows compila y ejecuta estas pruebas.

## Código

| Componente | Responsabilidad |
|---|---|
| `Program` | Opciones, una sola instancia, señal de parada y acceso directo de Inicio |
| `Native` | Lecturas nativas de RAM/CPU y avisos de Windows |
| `Counters` | Contadores PDH de discos, paginación y memoria GPU |
| `PowerWindow` | Ventana invisible que recibe avisos de pantalla y energía |
| `MonitorContext` | Bucle de muestreo, procesos, salud de escritura e icono de bandeja |
| `LogStore` | CSV/JSONL, rotación, vaciado y limpieza limitada a logs propios |
| `EventCollector` | Eventos filtrados, cursor persistente y campos técnicos permitidos |

Lee [cómo funciona y por qué se eligió C#](docs/architecture.md).
