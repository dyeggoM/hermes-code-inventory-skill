# Inventario por página (sitios HTML estáticos)

## Cuándo

El proyecto es un sitio servido como archivos: HTML + CSS + JS sin build tool, o generado por un
script (`generar_fichas.py`, `build.py`, un generador de fichas). La unidad de trabajo no es una
función sino una **página**: quién la produce, qué secciones tiene, qué assets carga y de qué datos
depende.

Úsalo cuando la pregunta sea «¿qué páginas existen?», «¿qué secciones tiene esta ficha?», «¿qué
páginas se rompen si cambio `components.css`?» o «dame el mapa de esta página, no del repositorio».

No lo uses para call graphs de JS de aplicación (para eso está el índice por símbolo) ni para un
proyecto con framework y build (ahí las páginas son artefactos, no fuente).

## Comandos

```bash
python3 scripts/page_inventory.py sync  --root /path/to/site   # indexa y escribe el mapa
python3 scripts/page_inventory.py stats --root /path/to/site   # totales + páginas sin título/h1
python3 scripts/page_inventory.py list  --root /path/to/site [--filtro "equipos/"]
python3 scripts/page_inventory.py show  equipos/lp-galaxy-conga.html --root /path/to/site
python3 scripts/page_inventory.py search --text "artistas" --root /path/to/site
```

`show` imprime: título, meta description, canonical, **plantilla que la genera**, tamaño y hash,
esquema de encabezados hasta `h3`, secciones con su `id` y clase, CSS/JS/iconos/imágenes y conteo de
enlaces y formularios. Es lo que se le pasa a un agente en vez del HTML.

## Salidas

- `<root>/.page-inventory/pages.json` — índice completo, una entrada por página (incluye `sha1`,
  `mtime`, `secciones`, `encabezados`, `ids`, `enlaces`, `plantilla`).
- `<root>/.page-inventory/PAGES.md` — tabla de una línea por página, para pegarle a un LLM.

El índice **no es fuente**: agrega `.page-inventory/` al `.gitignore` del proyecto. Un JSON de
megabytes sin ignorar lo lee cualquier recorrido de `git ls-files` (un escáner de seguridad, un
linter) y gasta presupuesto en un archivo de máquina.

## Linaje de la página (campo `plantilla`)

Se detecta por heurística, con puntaje: el script que declara la carpeta como salida
(`SALIDA = os.path.join(REPO, "equipos")`) gana sobre el que solo la menciona. Si el proyecto tiene
dos scripts que tocan la misma carpeta (un generador y un aplicador de contenido), revisa el valor
antes de creerlo: la heurística acierta cuando el generador declara su `SALIDA`.

Cuando la página **se genera**, editar el HTML es trabajo perdido: se cambia la plantilla y se vuelve
a correr el generador, y después `sync` de nuevo para que el índice refleje el HTML real.

## Pitfalls

- **Re-sincroniza después de regenerar.** El índice guarda el hash de cada página; tras correr el
  generador, `show` sobre una página vieja describe el archivo anterior. `sync` es idempotente.
- **Páginas dinámicas con query string** (`equipo.html?slug=…`) son una sola página para el índice:
  su mapa dice qué contenedores tiene (`#resumen`, `#artistas`), no qué fichas sirve.
- **Un HTML roto no debe tumbar el índice**: el parser captura la excepción y sigue con la siguiente
  página.
- **Rutas relativas**: el filtro de archivos ignorados se aplica a la ruta relativa al `root`, no a
  la absoluta — un proyecto que vive bajo `~/.algo/` se indexaría como vacío si se filtrara la
  absoluta.
- El mapa de una página incluye también el header y el footer (sus `h2`/enlaces). Al leer el esquema,
  distingue lo propio de la página de lo compartido por la plantilla.
