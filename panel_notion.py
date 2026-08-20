#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
panel_notion.py — genera el panel personal a partir de bases de datos de Notion.

Sin dependencias externas: sólo la librería estándar de Python 3.8+.

Uso:
    python panel_notion.py --esquema     # muestra las propiedades de tus bases
    python panel_notion.py               # genera el panel
    python panel_notion.py --publicar    # genera y sube a GitHub Pages por API

El token de Notion se toma, en este orden:
    1. la variable de entorno NOTION_TOKEN  (la usa GitHub Actions)
    2. el campo notion_token de config.json (uso local)

Así el mismo config.json sirve en tu PC y en el repositorio público,
donde el campo va vacío y el token vive como "secret" cifrado.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

AQUI = os.path.dirname(os.path.abspath(__file__))
NOTION_API = "https://api.notion.com/v1"


# --------------------------------------------------------------------------
# utilidades
# --------------------------------------------------------------------------
def morir(msg):
    print("\nERROR: " + msg + "\n", file=sys.stderr)
    sys.exit(1)


def cargar_config():
    ruta = os.path.join(AQUI, "config.json")
    if not os.path.exists(ruta):
        morir(
            "No encuentro config.json.\n"
            "Copia config.example.json a config.json y rellena tus datos."
        )
    with open(ruta, encoding="utf-8") as f:
        cfg = json.load(f)

    # la variable de entorno gana sobre el archivo
    tok = os.environ.get("NOTION_TOKEN", "").strip() or cfg.get("notion_token", "").strip()
    if not (tok.startswith("ntn_") or tok.startswith("secret_")):
        morir(
            "No tengo un token de Notion válido.\n"
            "Ponlo en notion_token dentro de config.json, o en la variable de\n"
            "entorno NOTION_TOKEN. Debe empezar por ntn_."
        )
    cfg["notion_token"] = tok

    g = cfg.setdefault("github", {})
    g["token"] = os.environ.get("GITHUB_TOKEN", "").strip() or g.get("token", "")
    return cfg


def limpiar_id(valor):
    """Acepta un ID pelado o una URL de Notion y devuelve el ID de 32 hex."""
    if not valor:
        return None
    v = valor.strip().split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
    v = "".join(c for c in v if c in "0123456789abcdefABCDEF")
    if len(v) > 32:
        v = v[-32:]
    if len(v) != 32:
        morir("No reconozco este ID de base de datos: %r" % valor)
    return v


def notion(cfg, ruta, metodo="POST", cuerpo=None):
    """Llamada a la API de Notion con reintentos y respeto del rate limit."""
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    req = urllib.request.Request(
        NOTION_API + ruta,
        data=datos,
        method=metodo,
        headers={
            "Authorization": "Bearer " + cfg["notion_token"],
            "Notion-Version": cfg.get("notion_version", "2022-06-28"),
            "Content-Type": "application/json",
        },
    )
    for intento in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            texto = e.read().decode("utf-8", "replace")
            if e.code == 429:
                time.sleep(float(e.headers.get("Retry-After", 2)))
                continue
            if e.code in (500, 502, 503, 504) and intento < 4:
                time.sleep(2 ** intento)
                continue
            if e.code == 404:
                morir(
                    "Notion devuelve 404 en %s.\n"
                    "O el ID no corresponde a una base de datos, o no la compartiste\n"
                    "con tu integración. Ejecuta listar_bases.py para verlo." % ruta
                )
            if e.code == 401:
                morir("Notion rechaza el token (401). Revisa notion_token / NOTION_TOKEN.")
            morir("Notion respondió %s en %s:\n%s" % (e.code, ruta, texto))
        except urllib.error.URLError as e:
            if intento < 4:
                time.sleep(2 ** intento)
                continue
            morir("No pude conectar con Notion: %s" % e)
    morir("Notion no respondió tras varios intentos.")


def consultar_bd(cfg, bd_id):
    """Devuelve todas las páginas de una base de datos (paginado incluido)."""
    filas, cursor = [], None
    while True:
        cuerpo = {"page_size": 100}
        if cursor:
            cuerpo["start_cursor"] = cursor
        r = notion(cfg, "/databases/%s/query" % bd_id, cuerpo=cuerpo)
        filas.extend(r.get("results", []))
        if not r.get("has_more"):
            return filas
        cursor = r["next_cursor"]
        time.sleep(0.35)  # el límite de Notion es ~3 req/s


# --------------------------------------------------------------------------
# lectura de propiedades
# --------------------------------------------------------------------------
def valor(prop):
    """Convierte cualquier propiedad de Notion en texto / lista / fecha."""
    if prop is None:
        return None
    t = prop.get("type")
    if t == "title":
        return "".join(x["plain_text"] for x in prop["title"]).strip() or None
    if t == "rich_text":
        return "".join(x["plain_text"] for x in prop["rich_text"]).strip() or None
    if t == "select":
        return prop["select"]["name"] if prop["select"] else None
    if t == "status":
        return prop["status"]["name"] if prop["status"] else None
    if t == "multi_select":
        return [o["name"] for o in prop["multi_select"]]
    if t == "people":
        return [p.get("name") or "(sin nombre)" for p in prop["people"]]
    if t == "date":
        return prop["date"]["start"] if prop["date"] else None
    if t == "checkbox":
        return prop["checkbox"]
    if t == "number":
        return prop["number"]
    if t == "url":
        return prop["url"]
    if t == "relation":
        return [r["id"].replace("-", "") for r in prop["relation"]]
    if t == "formula":
        f = prop["formula"]
        return f.get(f["type"])
    if t == "rollup":
        rl = prop["rollup"]
        if rl["type"] == "array":
            planos = []
            for x in rl["array"]:
                v = valor(x)
                planos.extend(v if isinstance(v, list) else [v])
            return [x for x in planos if x]
        return rl.get(rl["type"])
    return None


def buscar_prop(pagina, nombre, tipos=()):
    """Por nombre exacto, luego ignorando mayúsculas, y sólo si no se pidió
    nombre, por tipo. Si pediste un nombre y no existe, devuelve None: así
    un nombre mal escrito se nota en vez de agarrar otra propiedad al azar."""
    props = pagina["properties"]
    if nombre:
        if nombre in props:
            return props[nombre]
        objetivo = nombre.strip().lower()
        for k, v in props.items():
            if k.strip().lower() == objetivo:
                return v
        return None
    for v in props.values():
        if v.get("type") in tipos:
            return v
    return None


def titulo_de(pagina):
    for v in pagina["properties"].values():
        if v.get("type") == "title":
            return valor(v) or "(sin título en Notion)"
    return "(sin título en Notion)"


def como_lista(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def pasa_filtro(pagina, filtro):
    """filtro = {"propiedad": "Categoría P.A.R.A.", "valores": ["Proyectos"]}"""
    if not filtro or not filtro.get("propiedad"):
        return True
    permitidos = {str(v).strip().lower() for v in filtro.get("valores", [])}
    if not permitidos:
        return True
    for item in como_lista(valor(buscar_prop(pagina, filtro["propiedad"]))):
        if item and str(item).strip().lower() in permitidos:
            return True
    return False


# --------------------------------------------------------------------------
# modo --esquema
# --------------------------------------------------------------------------
def mostrar_esquema(cfg):
    for clave in ("tareas", "proyectos"):
        bd_id = limpiar_id(cfg[clave]["database_id"])
        info = notion(cfg, "/databases/%s" % bd_id, metodo="GET")
        nombre = "".join(x["plain_text"] for x in info.get("title", []))
        print("\n" + "=" * 64)
        print("%s  →  «%s»" % (clave.upper(), nombre or bd_id))
        print("=" * 64)
        for k, v in sorted(info["properties"].items()):
            extra = ""
            if v["type"] in ("select", "status", "multi_select"):
                opciones = (v[v["type"]] or {}).get("options") or []
                nombres = [o["name"] for o in opciones][:12]
                if nombres:
                    extra = "   opciones: " + ", ".join(nombres)
            print("  %-32s %-14s%s" % (k, v["type"], extra))
    print("\nCopia estos nombres a la sección \"props\" de config.json.\n")


# --------------------------------------------------------------------------
# construcción de datos
# --------------------------------------------------------------------------
def construir(cfg):
    tcfg, pcfg = cfg["tareas"], cfg["proyectos"]
    id_tareas = limpiar_id(tcfg["database_id"])
    id_proy = limpiar_id(pcfg["database_id"])

    print("Leyendo proyectos…")
    paginas_proy = consultar_bd(cfg, id_proy)
    print("Leyendo tareas…")
    paginas_tar = consultar_bd(cfg, id_tareas)

    # ---------- proyectos ----------
    pp = pcfg.get("props", {})
    filtro_proy = pcfg.get("solo_incluir")
    excluir_proy = {e.lower() for e in pcfg.get("excluir_estados", [])}
    proyectos, por_id, descartados = [], {}, 0

    for pg in paginas_proy:
        if pg.get("archived") or pg.get("in_trash"):
            continue
        if not pasa_filtro(pg, filtro_proy):
            descartados += 1
            continue
        estado = valor(buscar_prop(pg, pp.get("estado"), ("status", "select")))
        if estado and estado.lower() in excluir_proy:
            continue
        entrega = valor(buscar_prop(pg, pp.get("entrega"), ("date",)))
        nombre = titulo_de(pg)
        proyectos.append(
            {
                "n": nombre,
                "e": estado or "—",
                "d": entrega[:10] if entrega else None,
                "u": pg.get("url") or "https://www.notion.so/" + pg["id"].replace("-", ""),
            }
        )
        por_id[pg["id"].replace("-", "")] = nombre

    if descartados:
        print("  (%d fichas descartadas por no ser proyectos)" % descartados)

    # ---------- tareas ----------
    tp = tcfg.get("props", {})
    filtro_tar = tcfg.get("solo_incluir")
    excluir_tar = {e.lower() for e in tcfg.get("excluir_estados", ["done"])}
    mapa_prio = {k.lower(): v for k, v in tcfg.get("mapa_prioridad", {}).items()}
    tareas = []

    for pg in paginas_tar:
        if pg.get("archived") or pg.get("in_trash"):
            continue
        if not pasa_filtro(pg, filtro_tar):
            continue
        estado = valor(buscar_prop(pg, tp.get("estado"), ("status",)))
        if estado and estado.lower() in excluir_tar:
            continue

        fecha = valor(buscar_prop(pg, tp.get("fecha"), ("date",)))
        con_hora = bool(fecha and len(fecha) > 10)

        prio_bruta = valor(buscar_prop(pg, tp.get("prioridad")))
        if isinstance(prio_bruta, list):
            prio_bruta = prio_bruta[0] if prio_bruta else None
        prio = mapa_prio.get((prio_bruta or "").lower(), (prio_bruta or "").lower() or None)
        if prio not in ("alta", "media", "baja"):
            prio = None

        contexto = valor(buscar_prop(pg, tp.get("contexto")))
        if isinstance(contexto, list):
            contexto = contexto[0] if contexto else None

        personas = como_lista(valor(buscar_prop(pg, tp.get("personas"), ("people",))))
        rel = como_lista(valor(buscar_prop(pg, tp.get("proyecto"), ("relation",))))
        proyecto = next((por_id[r] for r in rel if r in por_id), None)

        tareas.append(
            {
                "t": titulo_de(pg),
                "s": valor(buscar_prop(pg, tp.get("eisenhower"))) or estado,
                "p": prio,
                "c": contexto,
                "w": [w for w in personas if w],
                "d": fecha,
                "dt": con_hora,
                "proj": proyecto,
                "u": pg.get("url") or "https://www.notion.so/" + pg["id"].replace("-", ""),
            }
        )

    orden_estado = {"activo": 0, "futuro": 1, "en espera": 2, "en pausa": 3}
    proyectos.sort(key=lambda p: (orden_estado.get(p["e"].lower(), 9), p["n"].lower()))

    sin_proy = sum(1 for t in tareas if not t["proj"])
    print("  %d tareas abiertas · %d proyectos vivos" % (len(tareas), len(proyectos)))
    if sin_proy:
        print("  %d tareas sin proyecto asignado" % sin_proy)
    return tareas, proyectos


def escapar(obj, clave=None):
    """El panel inserta los textos con innerHTML: neutralizamos < y > .
    Las URLs (clave 'u') se dejan intactas porque van dentro de href."""
    if isinstance(obj, str):
        if clave == "u":
            return obj.replace('"', "%22")
        return obj.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if isinstance(obj, list):
        return [escapar(x, clave) for x in obj]
    if isinstance(obj, dict):
        return {k: escapar(v, k) for k, v in obj.items()}
    return obj


def escribir_html(cfg, tareas, proyectos):
    tareas, proyectos = escapar(tareas), escapar(proyectos)

    ruta_plantilla = os.path.join(AQUI, "plantilla.html")
    if not os.path.exists(ruta_plantilla):
        morir("Falta plantilla.html junto al script.")
    with open(ruta_plantilla, encoding="utf-8") as f:
        html = f.read()

    ahora = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    bloque = "\n".join(
        [
            "// datos leídos de Notion el %s (UTC)" % ahora,
            "const TASKS = " + json.dumps(tareas, ensure_ascii=False, indent=1) + ";",
            "",
            "const PROJECTS = " + json.dumps(proyectos, ensure_ascii=False, indent=1) + ";",
            "",
            'const SYNC_ISO = "%s";' % ahora,
            'const TZ = "%s";' % cfg.get("zona_horaria", "America/Mexico_City"),
        ]
    ).replace("</script", "<\\/script")

    html = html.replace("/*__DATOS_NOTION__*/", bloque)
    html = html.replace("__TITULO__", cfg.get("titulo", "Panel personal"))
    html = html.replace(
        "__URL_DB_TAREAS__",
        "https://www.notion.so/" + limpiar_id(cfg["tareas"]["database_id"]),
    )
    html = html.replace(
        "__URL_DB_PROYECTOS__",
        "https://www.notion.so/" + limpiar_id(cfg["proyectos"]["database_id"]),
    )

    salida = os.path.join(AQUI, cfg.get("archivo_salida", "panel.html"))
    with open(salida, "w", encoding="utf-8") as f:
        f.write(html)
    print("Panel escrito en: %s" % salida)
    return salida


# --------------------------------------------------------------------------
# publicación por API (sólo para uso local; en Actions no hace falta)
# --------------------------------------------------------------------------
def github(cfg, ruta, metodo="GET", cuerpo=None):
    g = cfg["github"]
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    req = urllib.request.Request(
        "https://api.github.com" + ruta,
        data=datos,
        method=metodo,
        headers={
            "Authorization": "Bearer " + g["token"],
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "panel-notion",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        if e.code == 404 and metodo == "GET":
            return None
        morir(
            "GitHub respondió %s en %s:\n%s"
            % (e.code, ruta, e.read().decode("utf-8", "replace"))
        )


def publicar(cfg, ruta_html):
    g = cfg.get("github") or {}
    if not g.get("token") or not g.get("repo"):
        morir('Para publicar necesitas rellenar la sección "github" de config.json.')
    destino = g.get("ruta", "index.html")
    api = "/repos/%s/contents/%s" % (g["repo"], destino)

    with open(ruta_html, "rb") as f:
        contenido = base64.b64encode(f.read()).decode("ascii")

    actual = github(cfg, api + "?ref=" + g.get("rama", "main"))
    cuerpo = {
        "message": "Actualiza el panel — %s" % datetime.now().strftime("%Y-%m-%d %H:%M"),
        "content": contenido,
        "branch": g.get("rama", "main"),
    }
    if actual and actual.get("sha"):
        cuerpo["sha"] = actual["sha"]

    github(cfg, api, metodo="PUT", cuerpo=cuerpo)
    usuario, repo = g["repo"].split("/")
    print(
        "Publicado. En 1-2 minutos estará en:\n  https://%s.github.io/%s/%s"
        % (usuario, repo, destino)
    )


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Genera el panel personal desde Notion.")
    ap.add_argument("--esquema", action="store_true", help="muestra las propiedades de tus bases")
    ap.add_argument("--publicar", action="store_true", help="sube el panel a GitHub Pages")
    args = ap.parse_args()

    cfg = cargar_config()
    if args.esquema:
        mostrar_esquema(cfg)
        return

    tareas, proyectos = construir(cfg)
    ruta = escribir_html(cfg, tareas, proyectos)
    if args.publicar or cfg.get("publicar_siempre"):
        publicar(cfg, ruta)


if __name__ == "__main__":
    main()
