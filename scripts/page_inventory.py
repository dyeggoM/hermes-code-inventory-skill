#!/usr/bin/env python3
"""Inventario por PÁGINA para sitios estáticos (HTML/CSS/JS sin build).

Por qué existe: en un sitio estático la unidad de trabajo no es la función sino la página:
quién la genera, qué secciones tiene, qué CSS/JS carga y de qué datos depende. Con el índice
de páginas un agente abre el mapa de UNA página (`show`) en vez de leer todo el árbol.

Uso:
    python3 scripts/page_inventory.py sync  --root /path/to/site
    python3 scripts/page_inventory.py list  [--root .] [--filtro PATRON]
    python3 scripts/page_inventory.py show   equipos/lp-galaxy-conga.html [--root .]
    python3 scripts/page_inventory.py search --text "artistas" [--root .]
    python3 scripts/page_inventory.py outline <pagina>   # solo títulos e ids
    python3 scripts/page_inventory.py stats [--root .]

Salidas en <root>/.page-inventory/:
    pages.json   índice completo (una entrada por página)
    PAGES.md     una línea por página, para pegarle a un LLM

Sin dependencias: solo la librería estándar. Idempotente: reescribe lo que cambió.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

VERSION = "1.0.0"
IGNORAR_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".page-inventory",
                "assets/img", "dist", "build", ".cache"}
IGNORAR_ARCHIVOS = re.compile(r"(^|/)(_|\.)", re.I)


class MapaPagina(HTMLParser):
    """Extrae la estructura de una página: títulos, secciones, assets y datos."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.titulo = ""
        self.descripcion = ""
        self.canonical = ""
        self.lang = ""
        self.h1: list[str] = []
        self.encabezados: list[dict] = []
        self.secciones: list[dict] = []
        self.ids: list[str] = []
        self.css: list[str] = []
        self.js: list[str] = []
        self.iconos: list[str] = []
        self.imagenes: list[str] = []
        self.enlaces: list[str] = []
        self.formularios = 0
        self.scripts_inline = 0
        self._captura: str | None = None
        self._buffer: list[str] = []
        self._nivel = 0
        self._id_actual = ""

    # -- helpers ---------------------------------------------------------
    def _cerrar_captura(self) -> None:
        texto = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
        if self._captura == "title":
            self.titulo = texto
        elif self._captura == "h1":
            if texto:
                self.h1.append(texto)
        elif self._captura == "hx":
            self.encabezados.append({"nivel": self._nivel, "texto": texto,
                                     "id": self._id_actual or None})
            if self._nivel == 1 and texto:
                self.h1.append(texto)
        self._captura = None
        self._buffer = []
        self._id_actual = ""

    # -- HTMLParser ------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if a.get("id"):
            self.ids.append(a["id"])
        if tag == "html" and a.get("lang"):
            self.lang = a["lang"]
        elif tag == "meta" and a.get("name") == "description":
            self.descripcion = a.get("content", "").strip()
        elif tag == "link":
            rel = a.get("rel", "")
            href = a.get("href", "")
            if "canonical" in rel:
                self.canonical = href
            elif "stylesheet" in rel:
                self.css.append(href)
            elif "icon" in rel:
                self.iconos.append(href)
        elif tag == "script" and a.get("src"):
            self.js.append(a["src"])
        elif tag == "script":
            self.scripts_inline += 1
        elif tag == "img" and a.get("src"):
            self.imagenes.append(a["src"])
        elif tag == "a" and a.get("href"):
            self.enlaces.append(a["href"])
        elif tag == "form":
            self.formularios += 1
        elif tag == "section":
            self.secciones.append({"id": a.get("id") or None,
                                   "titulo": a.get("aria-labelledby") or None,
                                   "clase": a.get("class") or None})
        elif tag in ("title", "h1", "h2", "h3", "h4"):
            self._captura = "title" if tag == "title" else ("h1" if tag == "h1" else "hx")
            self._nivel = int(tag[1]) if tag.startswith("h") else 0
            self._id_actual = a.get("id", "")
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if self._captura and tag in ("title", "h1", "h2", "h3", "h4"):
            self._cerrar_captura()

    def handle_data(self, data: str) -> None:
        if self._captura:
            self._buffer.append(data)


def es_pagina(rel: str) -> bool:
    """La ruta es relativa al root: el directorio del proyecto puede llamarse `.algo`."""
    return rel.lower().endswith((".html", ".htm")) and not IGNORAR_ARCHIVOS.search("/" + rel)


def recorrer_css_js(root: Path) -> dict:
    hojas, scripts = [], []
    for base, dirs, ficheros in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORAR_DIRS]
        for f in ficheros:
            p = Path(base) / f
            rel = p.relative_to(root).as_posix()
            if p.suffix == ".css":
                hojas.append(rel)
            elif p.suffix == ".js":
                scripts.append(rel)
    return {"css": sorted(hojas), "js": sorted(scripts)}


def detectar_fuentes(root: Path) -> dict:
    """Qué genera o alimenta las páginas: scripts de build y datos JSON."""
    generadores, datos = [], []
    for base, dirs, ficheros in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORAR_DIRS]
        for f in ficheros:
            rel = (Path(base) / f).relative_to(root).as_posix()
            if f.endswith(".py") and re.search(r"gener|ficha|build|render|aplicar", f, re.I):
                generadores.append(rel)
            if f.endswith(".json") and "node_modules" not in rel:
                datos.append(rel)
    return {"generadores": sorted(generadores), "datos": sorted(datos)}


def plantilla_de(rel: str, fuentes: dict, root: Path) -> str | None:
    """Linaje heurístico: qué script produce esta página.

    Puntúa más fuerte que el script declare la carpeta como SALIDA (`SALIDA = ... "equipos"`)
    y menos que solo la mencione, para no confundir el generador con un aplicador de contenido.
    """
    partes = rel.split("/")
    if len(partes) < 2:
        return None
    carpeta = partes[0]
    mejor, puntos = None, 0
    for g in fuentes["generadores"]:
        try:
            texto = (root / g).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        marca = 0
        if re.search(r"SALIDA\s*=\s*os\.path\.join\([^)]*[\"']%s[\"']" % re.escape(carpeta), texto):
            marca = 5
        elif re.search(r"[\"']%s/[\"']" % re.escape(carpeta), texto):
            marca = 2
        elif re.search(r"[\"']%s[\"']" % re.escape(carpeta), texto):
            marca = 1
        if re.search(r"(escrib|write|open\()[^\n]*%s" % re.escape(carpeta), texto):
            marca += 1
        if marca > puntos:
            mejor, puntos = g, marca
    return mejor


def extraer(root: Path, ruta: Path) -> dict:
    html = ruta.read_text(encoding="utf-8", errors="replace")
    parser = MapaPagina()
    try:
        parser.feed(html)
    except Exception:  # un HTML roto no debe tumbar el índice
        pass
    rel = ruta.relative_to(root).as_posix()
    return {
        "ruta": rel,
        "bytes": ruta.stat().st_size,
        "sha1": hashlib.sha1(html.encode("utf-8", "replace")).hexdigest()[:12],
        "mtime": datetime.fromtimestamp(ruta.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "titulo": parser.titulo,
        "descripcion": parser.descripcion,
        "canonical": parser.canonical,
        "lang": parser.lang,
        "h1": parser.h1,
        "encabezados": parser.encabezados,
        "secciones": parser.secciones,
        "ids": parser.ids,
        "css": parser.css,
        "js": parser.js,
        "iconos": parser.iconos,
        "imagenes": parser.imagenes[:20],
        "enlaces": parser.enlaces,
        "formularios": parser.formularios,
        "scripts_inline": parser.scripts_inline,
        "texto_plano": len(re.sub(r"<[^>]+>", " ", html.split("</head>")[-1])),
    }


def sync(root: Path, salida: Path, verbose: bool = True) -> dict:
    fuentes = detectar_fuentes(root)
    fuentes.update(recorrer_css_js(root))
    paginas = []
    for base, dirs, ficheros in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORAR_DIRS)
        for f in sorted(ficheros):
            ruta = Path(base) / f
            rel = ruta.relative_to(root).as_posix()
            if not es_pagina(rel):
                continue
            pagina = extraer(root, ruta)
            pagina["plantilla"] = plantilla_de(pagina["ruta"], fuentes, root)
            paginas.append(pagina)

    indice = {
        "version": VERSION,
        "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": str(root),
        "totales": {
            "paginas": len(paginas),
            "css": len(fuentes["css"]),
            "js": len(fuentes["js"]),
            "formularios": sum(p["formularios"] for p in paginas),
        },
        "fuentes": fuentes,
        "paginas": paginas,
    }
    salida.mkdir(parents=True, exist_ok=True)
    (salida / "pages.json").write_text(json.dumps(indice, ensure_ascii=False, indent=1), encoding="utf-8")
    (salida / "PAGES.md").write_text(render_markdown(indice), encoding="utf-8")
    if verbose:
        print("páginas indexadas: %d" % len(paginas))
        print("css: %d · js: %d · formularios: %d" % (len(fuentes["css"]), len(fuentes["js"]),
                                                      indice["totales"]["formularios"]))
        print("salida: %s/pages.json + PAGES.md" % salida)
    return indice


def render_markdown(indice: dict) -> str:
    lineas = [
        "# Inventario por página",
        "",
        "Generado: %s · %d páginas · root: `%s`" % (indice["generado"], len(indice["paginas"]), indice["root"]),
        "",
        "Consulta una página con `python3 scripts/page_inventory.py show <ruta>`; no cargues todo el árbol.",
        "",
        "| Página | Título | Secciones | CSS/JS | Plantilla | bytes |",
        "|---|---|---|---|---|---|",
    ]
    for p in indice["paginas"]:
        secciones = ", ".join((s["id"] or "—") for s in p["secciones"]) or "—"
        assets = "%d/%d" % (len(p["css"]), len(p["js"]))
        lineas.append("| `%s` | %s | %s | %s | %s | %d |" % (
            p["ruta"], (p["titulo"] or "—")[:70].replace("|", "/"), secciones, assets,
            ("`%s`" % p["plantilla"]) if p["plantilla"] else "—", p["bytes"]))
    lineas += ["", "## Fuentes", "",
               "Generadores: %s" % (", ".join("`%s`" % g for g in indice["fuentes"]["generadores"]) or "—"),
               "", "Datos: %s" % (", ".join("`%s`" % d for d in indice["fuentes"]["datos"]) or "—"), ""]
    return "\n".join(lineas)


def cargar(root: Path, salida: Path) -> dict:
    ruta = salida / "pages.json"
    if not ruta.exists():
        return sync(root, salida, verbose=False)
    return json.loads(ruta.read_text(encoding="utf-8"))


def cmd_show(indice: dict, objetivo: str) -> int:
    objetivo = objetivo.lstrip("./")
    paginas = indice["paginas"]
    exacta = [p for p in paginas if p["ruta"] == objetivo or p["ruta"].endswith("/" + objetivo)]
    if not exacta:
        parecidas = [p["ruta"] for p in paginas if objetivo.lower() in p["ruta"].lower()][:10]
        print("no encontrada: %s" % objetivo)
        if parecidas:
            print("parecidas: " + ", ".join(parecidas))
        return 1
    p = exacta[0]
    print("PÁGINA  %s" % p["ruta"])
    print("título  %s" % (p["titulo"] or "—"))
    if p["descripcion"]:
        print("meta    %s" % p["descripcion"][:160])
    if p["canonical"]:
        print("canon.  %s" % p["canonical"])
    print("plantilla %s" % (p["plantilla"] or "— (escrita a mano)"))
    print("tamaño  %d bytes · %d car. de texto · sha %s" % (p["bytes"], p["texto_plano"], p["sha1"]))
    print("\nESQUEMA")
    for h in p["encabezados"]:
        if h["nivel"] <= 3:
            print("  %s%s%s" % ("  " * (h["nivel"] - 1), "h%d " % h["nivel"], h["texto"][:80]))
    print("\nSECCIONES (%d)" % len(p["secciones"]))
    for s in p["secciones"]:
        print("  #%-28s %s" % (s["id"] or "—", (s["clase"] or "")[:60]))
    print("\nASSETS")
    print("  css: %s" % (", ".join(p["css"]) or "—"))
    print("  js : %s" % (", ".join(p["js"]) or "—"))
    if p["iconos"]:
        print("  ico: %s" % ", ".join(p["iconos"]))
    if p["imagenes"]:
        print("  img: %d (%s)" % (len(p["imagenes"]), ", ".join(p["imagenes"][:4])))
    print("\nENLACES  %d · formularios %d · scripts inline %d" % (
        len(p["enlaces"]), p["formularios"], p["scripts_inline"]))
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Inventario por página para sitios estáticos")
    ap.add_argument("accion", choices=["sync", "list", "show", "outline", "search", "stats"])
    ap.add_argument("objetivo", nargs="?", default="")
    ap.add_argument("--root", default=".")
    ap.add_argument("--filtro", default="")
    ap.add_argument("--text", default="")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    salida = root / ".page-inventory"
    if args.accion == "sync":
        sync(root, salida)
        return 0

    indice = cargar(root, salida)
    if args.accion == "list":
        pat = re.compile(args.filtro, re.I) if args.filtro else None
        for p in indice["paginas"]:
            if pat and not (pat.search(p["ruta"]) or pat.search(p["titulo"] or "")):
                continue
            print("%-52s %-58s %s" % (p["ruta"], (p["titulo"] or "—")[:56], p["plantilla"] or ""))
        return 0
    if args.accion == "show":
        return cmd_show(indice, args.objetivo)
    if args.accion == "outline":
        return cmd_show(indice, args.objetivo)
    if args.accion == "search":
        if not args.text:
            print("falta --text"); return 2
        aguja = args.text.lower()
        for p in indice["paginas"]:
            campos = [p["ruta"], p["titulo"] or "", p["descripcion"] or ""]
            campos += [h["texto"] for h in p["encabezados"]]
            campos += [s["id"] or "" for s in p["secciones"]]
            if any(aguja in c.lower() for c in campos):
                print("%-52s %s" % (p["ruta"], (p["titulo"] or "—")[:56]))
        return 0
    if args.accion == "stats":
        t = indice["totales"]
        print("páginas: %d · css: %d · js: %d · formularios: %d" % (
            t["paginas"], t["css"], t["js"], t["formularios"]))
        sin_titulo = [p["ruta"] for p in indice["paginas"] if not p["titulo"]]
        sin_h1 = [p["ruta"] for p in indice["paginas"] if not p["h1"]]
        huerfanas = [p["ruta"] for p in indice["paginas"] if not p["enlaces"]]
        print("sin <title>: %d%s" % (len(sin_titulo), (" → " + ", ".join(sin_titulo[:5])) if sin_titulo else ""))
        print("sin h1: %d%s" % (len(sin_h1), (" → " + ", ".join(sin_h1[:5])) if sin_h1 else ""))
        print("sin enlaces salientes: %d" % len(huerfanas))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
