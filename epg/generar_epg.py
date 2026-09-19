#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generador de EPG externo para Prometeus 3.27.5
================================================
Agrega fuentes EXTERNAS de guía (sin depender de metadata embebida en los
streams) y publica epg.json compacto para la app Android TV.

Fuentes (todas públicas):
  · epgshare01.online  — feeds XMLTV gz: AR1, UY1, DIRECTVSPORTS1
  · mi.tv              — endpoint async HTML por canal/día (hora local AR)

Anclas de tiempo: cada feed escribe horas "crudas" con offsets declarados
INCORRECTOS (medido empíricamente). Este script RE-CALIBRA las anclas en
cada corrida contra mi.tv (verdad de referencia, hora local argentina) y
aplica el corrimiento medido. Si mi.tv no responde, usa las anclas estáticas
medidas (ar1=+8h, uy1p=+6h, uy1b=+3h, dtvs=+3h respecto de la hora cruda
interpretada como UTC).

Salida: epg.json {v, gen, exp, n, canales:{nombre:[[inicio,fin,título,desc?]..]}}
con épocas en SEGUNDOS UTC. Solo canales de las 8 playlists pedidas.
"""
import gzip
import html
import json
import os
import re
import subprocess
import sys
import time
import datetime
import collections

AQUI = os.path.dirname(os.path.abspath(__file__))
AR_TZ = datetime.timezone(datetime.timedelta(hours=-3))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/139"

FEEDS = {
    "ar1": "https://epgshare01.online/epgshare01/epg_ripper_AR1.xml.gz",
    "uy1": "https://epgshare01.online/epgshare01/epg_ripper_UY1.xml.gz",
    "dtvs": "https://epgshare01.online/epgshare01/epg_ripper_DIRECTVSPORTS1.xml.gz",
}
ANCHOR_DEFECTO = {"ar1": 8 * 3600, "uy1p": 6 * 3600, "uy1b": 3 * 3600, "dtvs": 3 * 3600}
DIAS = 2           # hoy + mañana (AR)
TTL_SEG = 6 * 3600  # la app refresca cada 6 h
DESC_MAX = 160

log = lambda *a: print(*a, flush=True)


# ───────────────────────── descarga ─────────────────────────

def descargar(url, destino, intentos=3):
    for i in range(intentos):
        try:
            r = subprocess.run(
                ["curl", "-sL", "--max-time", "120", "--compressed",
                 "-H", f"User-Agent: {UA}", "-o", destino, "-w", "%{http_code}", url],
                capture_output=True, text=True, timeout=150)
            if r.stdout.strip() == "200" and os.path.exists(destino) and os.path.getsize(destino) > 0:
                return True
            log(f"  descarga {url} → HTTP {r.stdout.strip()} (intento {i+1})")
        except Exception as e:
            log(f"  descarga error {e} (intento {i+1})")
        time.sleep(4)
    return False


def texto_gz(destino):
    """Lee un .xml.gz (o .xml si el servidor ya lo sirvió descomprimido)."""
    with open(destino, "rb") as f:
        raw = f.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "ignore")


_mitv_estado = {"fallos_seguidos": 0, "enfriado": False}


def mitv_canal(slug, fecha, cache={}):
    """Programas de mi.tv para un canal y fecha: [(hh:mm, título, cat, sinopsis)].
    Cachea por (slug, fecha). Si mi.tv bloquea (404 en ráfaga) enfría 90 s una vez."""
    if (slug, fecha) in cache:
        return cache[(slug, fecha)]
    progs = []
    for i in range(2):
        try:
            r = subprocess.run(
                ["curl", "-s", "--max-time", "20",
                 "-H", f"User-Agent: {UA}", "-H", "Accept: text/html",
                 f"https://mi.tv/ar/async/channel/{slug}/{fecha}/0"],
                capture_output=True, timeout=25)
            h = r.stdout.decode("utf-8", "ignore")
            progs = re.findall(
                r'<span class="time">(\d\d:\d\d)</span>\s*<h2>\s*(?:<a[^>]*>)?([^<\n\t]+)'
                r'.*?(?:<span class="sub-title">([^<]*)</span>)?\s*<p class="synopsis">\s*([^<]*)',
                h, re.S)
            progs = [(hm, html.unescape(t.strip()), html.unescape(c.strip() or ""),
                      html.unescape(s.strip())) for hm, t, c, s in progs]
        except Exception:
            progs = []
        if progs:
            _mitv_estado["fallos_seguidos"] = 0
            break
        # 404 en ráfaga = bloqueo temporal: no gastar reintentos, enfriar y seguir
        _mitv_estado["fallos_seguidos"] += 1
        if _mitv_estado["fallos_seguidos"] >= 4 and not _mitv_estado["enfriado"]:
            _mitv_estado["enfriado"] = True
            log("  mi.tv bloqueado — enfriando 90 s…")
            time.sleep(90)
        else:
            time.sleep(3)
        if _mitv_estado["fallos_seguidos"] >= 8:
            break  # canal bloqueado: al siguiente
    cache[(slug, fecha)] = progs
    return progs


# ───────────────────── calibración de anclas ─────────────────────

def ep_arlocal(fecha, hm):
    return int(datetime.datetime.strptime(fecha + hm, "%Y%m%d%H%M")
               .replace(tzinfo=AR_TZ).timestamp())


def raw_a_utc(st):
    return int(datetime.datetime.strptime(st, "%Y%m%d%H%M%S")
               .replace(tzinfo=datetime.timezone.utc).timestamp())


def norm(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())[:24]


def calibrar(mapa_progs, hoy):
    """Mide el corrimiento de cada familia contra mi.tv. Devuelve {familia: G}
    tal que época = raw_interpretada_como_UTC + G."""
    anchors = dict(ANCHOR_DEFECTO)
    # 1) AR1 directo contra mi.tv (El Trece)
    ref = {}
    for slug in ("el-trece", "espn", "tn"):
        p = mitv_canal(slug, hoy)
        if p:
            ref[slug] = collections.defaultdict(list)
            for hm, t, _, _ in p:
                ref[slug][norm(t)].append(ep_arlocal(hoy, hm))
        time.sleep(2)
    medidas = collections.defaultdict(list)

    def medir(familia, cid, slug):
        if slug not in ref or not ref[slug]:
            return
        feed = "uy1" if familia.startswith("uy1") else familia
        for st, _stop, t, _d in mapa_progs.get((feed, cid), []):
            for verdadero in ref[slug].get(norm(t), []):
                medidas[familia].append(verdadero - raw_a_utc(st))

    medir("ar1", "Canal.13.de.Argentina.(El.Trece).ar", "el-trece")
    medir("uy1p", "ESPN.HD.uy", "espn")
    medir("uy1p", "TN.HD.uy", "tn")

    for familia in ("ar1", "uy1p"):
        ds = medidas.get(familia)
        if ds and len(ds) >= 4:
            med = sorted(ds)[len(ds) // 2]
            if med % 900 == 0 or abs(med % 3600) < 600:  # tolerancia a minutos de redondeo
                anchors[familia] = med
                log(f"  ancla {familia}: CALIBRADA = +{med/3600:.2f}h (n={len(ds)})")
            else:
                log(f"  ancla {familia}: medida ruidosa {med/60:+.0f}min → estática")
        else:
            log(f"  ancla {familia}: sin pares ({len(ds or [])}) → estática +{anchors[familia]/3600:.0f}h")

    # 2) uy1b y dtvs: familia bracket = uy1p desplazada +3h en crudo (delta
    #    estructural medida entre familias: +180 min exactos en 19 canales),
    #    y DIRECTVSPORTS1 coincide a +0 min con la bracket → heredan ancla.
    anchors["uy1b"] = anchors["uy1p"] - 3 * 3600
    anchors["dtvs"] = anchors["uy1b"]
    log(f"  anclas derivadas: uy1b/dtvs = +{anchors['uy1b']/3600:.2f}h")
    return anchors


# ───────────────────── parseo XMLTV ─────────────────────

PROG_RE = re.compile(
    r'<programme start="(\d{14})\s+([+-]\d{4})"[^>]*stop="(\d{14})\s+[+-]\d{4}"[^>]*channel="([^"]+)">'
    r'(.*?)</programme>', re.S)
PROG_RE_SSTOP = re.compile(
    r'<programme start="(\d{14})\s+([+-]\d{4})"[^>]*channel="([^"]+)">(.*?)</programme>', re.S)
TIT_RE = re.compile(r"<title[^>]*>([^<]*)</title>")
DESC_RE = re.compile(r"<desc[^>]*>([^<]*)</desc>")
SUB_RE = re.compile(r"<sub-title[^>]*>([^<]*)</sub-title>")
CAT_RE = re.compile(r"<category[^>]*>([^<]*)</category>")


def parsear_xmltv(txt):
    """→ {(canal): [(start_raw, stop_raw|None, título, desc)]} — IDs des-escapados."""
    out = collections.defaultdict(list)
    vistos = set()
    for m in PROG_RE.finditer(txt):
        st, _off, sp, cid, cuerpo = m.groups()
        cid = html.unescape(cid)
        if (st, cid) in vistos:
            continue
        vistos.add((st, cid))
        t = TIT_RE.search(cuerpo)
        d = DESC_RE.search(cuerpo) or SUB_RE.search(cuerpo) or CAT_RE.search(cuerpo)
        out[cid].append((st, sp, html.unescape(t.group(1).strip()) if t else "",
                         html.unescape(d.group(1).strip()) if d else ""))
    for m in PROG_RE_SSTOP.finditer(txt):
        st, _off, cid, cuerpo = m.groups()
        cid = html.unescape(cid)
        if (st, cid) in vistos:
            continue
        vistos.add((st, cid))
        t = TIT_RE.search(cuerpo)
        d = DESC_RE.search(cuerpo) or SUB_RE.search(cuerpo)
        out[cid].append((st, None, html.unescape(t.group(1).strip()) if t else "",
                         html.unescape(d.group(1).strip()) if d else ""))
    return out


# ───────────────────── generación ─────────────────────

def main():
    ahora_ar = datetime.datetime.now(AR_TZ)
    hoy = ahora_ar.strftime("%Y%m%d")
    dias = [(ahora_ar + datetime.timedelta(days=d)).strftime("%Y%m%d") for d in range(DIAS)]
    inicio = int(datetime.datetime.strptime(dias[0], "%Y%m%d")
                 .replace(tzinfo=AR_TZ).timestamp())
    fin = inicio + DIAS * 86400
    log(f"Ventana EPG: {dias[0]} → {dias[-1]} (hora AR)")

    mapa = json.load(open(os.path.join(AQUI, "mapa.json"), encoding="utf-8"))["canales"]

    # 1) descarga feeds
    progs_feed = {}
    for fam, url in FEEDS.items():
        destino = os.path.join("/tmp", f"epgsrc_{fam}.xml.gz")
        if not descargar(url, destino):
            log(f"  ¡FALLA feed {fam}! continúa con el resto")
            continue
        progs_feed[fam] = parsear_xmltv(texto_gz(destino))
        n = sum(len(v) for v in progs_feed[fam].values())
        log(f"  feed {fam}: {len(progs_feed[fam])} canales · {n} programas")

    # 2) calibración de anclas (usa canales de referencia de los feeds + mi.tv)
    anclas = calibrar(progs_feed, hoy)
    log(f"  anclas finales: " + ", ".join(f"{k}=+{v/3600:.2f}h" for k, v in anclas.items()))

    # 3) construye la guía por canal del mapa
    guia = {}
    faltantes = []
    t0_mitv = time.time()
    MITV_TOPE = 14 * 60  # segundos máximos dedicados a mi.tv en esta corrida
    for nombre, (fuente, cid) in mapa.items():
        programas = []
        if fuente in ("ar1", "uy1p", "uy1b", "dtvs"):
            fam = "uy1" if fuente.startswith("uy1") else fuente
            if fam not in progs_feed or cid not in progs_feed[fam]:
                faltantes.append(f"{nombre} ({fuente}:{cid} no está en el feed)")
                continue
            G = anclas[fuente]
            for st, sp, t, d in progs_feed[fam][cid]:
                s = raw_a_utc(st) + G
                e = raw_a_utc(sp) + G if sp else s + 90 * 60
                if e <= s:
                    e = s + 30 * 60
                programas.append((s, e, t, d))
        elif fuente == "mitv":
            # descarga hoy y mañana del canal (con tope global de tiempo)
            if time.time() - t0_mitv > MITV_TOPE:
                faltantes.append(f"{nombre} (tope de tiempo mi.tv)")
                continue
            por_dia = []
            ok = False
            for fecha in dias:
                p = mitv_canal(cid, fecha)
                if p:
                    ok = True
                else:
                    break  # si hoy falló (bloqueo/canal caído), no insistir con mañana
                por_dia.append((fecha, p))
                time.sleep(2.5)
            if not ok:
                faltantes.append(f"{nombre} (mi.tv sin datos)")
                continue
            for i, (fecha, p) in enumerate(por_dia):
                for j, (hm, t, cat, sin) in enumerate(p):
                    s = ep_arlocal(fecha, hm)
                    # fin = próximo arranque (mismo día, siguiente día, o tope 6h)
                    if j + 1 < len(p):
                        e = ep_arlocal(fecha, p[j + 1][0])
                    elif i + 1 < len(por_dia) and por_dia[i + 1][1]:
                        e = ep_arlocal(dias[i + 1], por_dia[i + 1][1][0][0])
                    else:
                        e = s + 6 * 3600
                    if e <= s:
                        e = s + 30 * 60
                    desc = sin if sin and sin != t else cat
                    programas.append((s, e, t, desc))
        # recorte a la ventana + saneo
        programas = [(s, e, t.strip(), d.strip()) for s, e, t, d in programas
                     if e > inicio and s < fin and t]
        if not programas:
            faltantes.append(f"{nombre} (sin programas en ventana)")
            continue
        # dedupe por (s, título) y orden
        vistos, limpios = set(), []
        for s, e, t, d in sorted(programas):
            k = (s, norm(t))
            if k in vistos:
                continue
            vistos.add(k)
            limpios.append([s, min(e, fin), t[:90], d[:DESC_MAX] if d and d != t else ""])
        # fusiona solapamientos grotescos (>2h de solape con el mismo vecino)
        guia[nombre] = limpios

    # 4) salida
    exp = int(time.time()) + TTL_SEG
    doc = {"v": 1, "gen": int(time.time()), "exp": exp, "n": len(guia), "canales": guia}
    salida = os.path.join(AQUI, "..", "epg.json")
    with open(salida, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    total_progs = sum(len(v) for v in guia.values())
    log(f"\nEPG generado: {len(guia)}/{len(mapa)} canales · {total_progs} programas "
        f"· {os.path.getsize(salida)//1024} KB → {os.path.abspath(salida)}")
    if faltantes:
        log("Sin datos (fallback en la app):")
        for f in faltantes:
            log(f"  · {f}")
    if len(guia) < len(mapa) * 0.4:
        log("COBERTURA DEMASIADO BAJA — abortando para no publicar EPG vacío")
        sys.exit(2)


if __name__ == "__main__":
    main()
