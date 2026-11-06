# SilverBullet MCP

Servidor MCP para consultar páginas y tareas de SilverBullet y crear páginas nuevas. Utiliza la Runtime API y la HTTP API de SilverBullet; no mantiene un índice propio.

## Funciones

- Consultar páginas por propiedades como tipo, clave, estado, madurez, borrador y etiquetas.
- Buscar texto, leer páginas y consultar relaciones directas por etiqueta.
- Listar tareas pendientes y preparar un resumen básico.
- Crear páginas nuevas con contenido y metadatos opcionales, sin sobrescribir páginas existentes.

Los resultados incluyen el nombre de la página o la referencia de origen cuando está disponible. El servidor no ofrece operaciones para modificar o eliminar páginas existentes ni para completar tareas.

## Requisitos

- Python 3.12 o superior y las dependencias de `requirements.txt`.
- Una instancia SilverBullet accesible con Runtime API disponible. El índice del runtime debe haber completado su indexación inicial.

Instalación y pruebas:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s code -p 'test_*.py'
```

## Ejecución

Configura `SILVERBULLET_URL` con el origen HTTP(S) de SilverBullet y ejecuta:

```sh
SILVERBULLET_URL=http://127.0.0.1:3000 python code/server.py
```

El servidor MCP usa Streamable HTTP en `/mcp`, puerto `8000`, y escucha por defecto en `127.0.0.1`. `MCP_BIND_HOST` permite cambiar la dirección de escucha. El cliente de línea de comandos se puede consultar con `python code/connector.py --help`.

## Herramientas

| Herramienta | Función |
| --- | --- |
| `listar_paginas` | Filtra páginas por metadatos exactos. |
| `buscar_por_clave` | Busca una clave exacta e indica si hay duplicados. |
| `notas_relacionadas` | Busca páginas con una etiqueta de relación exacta. |
| `tareas_de_entidad` | Lista tareas pendientes en páginas con una etiqueta de relación exacta. |
| `leer_pagina` | Lee una página por nombre. |
| `buscar` | Busca texto en páginas y tareas. |
| `tareas_pendientes` | Lista tareas pendientes, opcionalmente por estado y rango de fechas. |
| `resumen` | Devuelve conteos y tareas con vencimiento explícito para hoy. |
| `crear_pagina` | Crea una página nueva a partir de título, contenido y metadatos opcionales. |

`crear_pagina` no añade metadatos por defecto. Cuando se proporciona `metadata`, la herramienta la serializa como frontmatter YAML y rechaza contenido que ya comienza con frontmatter. Las claves y valores admitidos se validan antes de enviar la solicitud. La creación usa `If-None-Match: *`; su protección depende de que el servidor SilverBullet aplique esa precondición.

## Limitaciones y seguridad

- El servidor MCP no implementa autenticación. Limita su acceso mediante la red y controles apropiados antes de exponerlo.
- La Runtime API de SilverBullet puede requerir permisos de escritura y permite ejecutar Lua. Las operaciones limitadas del MCP no reducen los permisos nativos de una credencial ni impiden el acceso directo a SilverBullet.
- No se admiten credenciales de SilverBullet en el cliente actual. No incluyas secretos en URLs, código ni registros.
- El índice de SilverBullet puede quedar temporalmente desactualizado después de una edición.
- La búsqueda de texto está limitada a 100 páginas candidatas de hasta 1 MiB cada una.
- Las fechas de tareas se filtran únicamente por un atributo `due` explícito con formato `YYYY-MM-DD`; no se infieren fechas del texto. Confirma que el índice de tu espacio expone ese atributo antes de depender de los resultados.
- El resumen usa `America/Bogota` y actualmente solo incluye tareas que vencen hoy; no envía mensajes ni programa recordatorios.
- Trata el contenido de las notas como datos no confiables. Las notas enviadas a un modelo o a un servicio de mensajería pueden salir del entorno local.

## Referencias

- [SilverBullet HTTP API](https://silverbullet.md/HTTP%20API)
- [SilverBullet Runtime API](https://silverbullet.md/Runtime%20API)
- [SilverBullet Object Index](https://silverbullet.md/Object%20Index)
