#!/usr/bin/env python3
"""
pdf_forense.py — Auditoría forense de integridad de documentos PDF

Analiza un PDF en busca de:

  1. Revisiones ocultas por actualización incremental (contenido de
     versiones anteriores que sigue físicamente en el archivo aunque un
     lector normal no lo muestre)
  2. Redacciones defectuosas (texto "tachado" con un rectángulo opaco que
     sigue siendo extraíble por debajo)
  3. Metadatos (software, fechas de creación/modificación)
  4. Presencia de firma digital y su relación con revisiones posteriores

IMPORTANTE: esta herramienta genera INDICIOS técnicos orientativos para
apoyar el trabajo de un perito informático. Ver "Límites" en el README.

Autora: Nieves Casquero — Perito Informático de Parte (Colegiada AEPEJU)
Licencia: MIT
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pypdf
import pdfplumber

VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# 1. Revisiones ocultas (actualizaciones incrementales)
# --------------------------------------------------------------------------- #

def encontrar_finales_de_revision(data: bytes):
    """Cada '%%EOF' precedido en las líneas anteriores por 'startxref'
    marca el final de una revisión guardada del archivo. Un PDF guardado
    una sola vez tiene un único bloque; cada 'Guardar' incremental posterior
    (típico de anotar, firmar o rellenar un formulario en Acrobat) añade otro."""
    posiciones = [m.end() for m in re.finditer(rb"%%EOF", data)]
    revisiones = []
    for pos in posiciones:
        ventana = data[max(0, pos - 200):pos]
        if b"startxref" in ventana:
            revisiones.append(pos)
    return revisiones


def extraer_texto_de_revision(data: bytes, fin_offset: int, tmp_path: Path):
    """Trunca el archivo justo al final de una revisión y trata de leerlo
    como un PDF independiente y válido en ese punto de su historia."""
    fragmento = data[:fin_offset]
    tmp_path.write_bytes(fragmento)
    try:
        reader = pypdf.PdfReader(str(tmp_path))
        texto = "\n".join((p.extract_text() or "") for p in reader.pages)
        return texto.strip()
    except Exception as e:
        return f"[No se pudo reconstruir esta revisión: {e}]"


def analizar_revisiones(path: Path, tmp_dir: Path):
    data = path.read_bytes()
    finales = encontrar_finales_de_revision(data)

    resultado = {
        "num_revisiones": len(finales),
        "revisiones": [],
    }

    textos = []
    for i, fin in enumerate(finales, start=1):
        tmp_path = tmp_dir / f"_rev_{i}.pdf"
        texto = extraer_texto_de_revision(data, fin, tmp_path)
        textos.append(texto)
        resultado["revisiones"].append({
            "revision": i,
            "tamano_hasta_aqui_bytes": fin,
            "texto_visible": texto,
        })
        tmp_path.unlink(missing_ok=True)

    # Marca si el texto cambia entre revisiones consecutivas
    resultado["contenido_difiere_entre_revisiones"] = len(set(textos)) > 1 if len(textos) > 1 else False
    return resultado


# --------------------------------------------------------------------------- #
# 2. Redacciones defectuosas
# --------------------------------------------------------------------------- #

def solapa(box_a, box_b, margen=1.0):
    ax0, atop, ax1, abottom = box_a
    bx0, btop, bx1, bbottom = box_b
    return not (ax1 < bx0 - margen or bx1 < ax0 - margen or
                abottom < btop - margen or bbottom < atop - margen)


def es_color_oscuro(color):
    if color is None:
        return False
    try:
        if isinstance(color, (int, float)):
            return color < 0.35
        vals = list(color)
        return all(v < 0.35 for v in vals)
    except TypeError:
        return False


def analizar_redacciones(path: Path):
    hallazgos = []
    with pdfplumber.open(str(path)) as pdf:
        for num_pagina, pagina in enumerate(pdf.pages, start=1):
            palabras = pagina.extract_words()
            rects_oscuros = [
                r for r in pagina.rects
                if r.get("fill") and es_color_oscuro(r.get("non_stroking_color"))
                and (r["x1"] - r["x0"]) * (r["bottom"] - r["top"]) > 25
            ]
            for rect in rects_oscuros:
                caja_rect = (rect["x0"], rect["top"], rect["x1"], rect["bottom"])
                palabras_debajo = [
                    p["text"] for p in palabras
                    if solapa((p["x0"], p["top"], p["x1"], p["bottom"]), caja_rect)
                ]
                if palabras_debajo:
                    hallazgos.append({
                        "pagina": num_pagina,
                        "posicion": [round(v, 1) for v in caja_rect],
                        "texto_oculto_recuperado": " ".join(palabras_debajo),
                    })
    return hallazgos


# --------------------------------------------------------------------------- #
# 3. Metadatos y firma
# --------------------------------------------------------------------------- #

def analizar_metadatos_y_firma(path: Path):
    reader = pypdf.PdfReader(str(path))
    meta = reader.metadata or {}
    meta_dict = {k.lstrip("/"): str(v) for k, v in meta.items()} if meta else {}

    tiene_firma = False
    try:
        root = reader.trailer["/Root"]
        if "/AcroForm" in root:
            acroform = root["/AcroForm"]
            if "/SigFlags" in acroform:
                tiene_firma = True
    except Exception:
        pass

    return meta_dict, tiene_firma


# --------------------------------------------------------------------------- #
# Informe
# --------------------------------------------------------------------------- #

def construir_informe(path, rev_info, redacciones, meta, tiene_firma):
    lines = []
    lines.append("# Informe de Auditoría Forense de PDF\n")
    lines.append(f"**Archivo analizado:** `{path.name}`  ")
    lines.append(f"**Fecha de análisis (UTC):** {datetime.now(timezone.utc).isoformat()}  ")
    lines.append(f"**Tamaño:** {path.stat().st_size} bytes\n")

    lines.append("## 1. Historial de revisiones (actualizaciones incrementales)\n")
    n = rev_info["num_revisiones"]
    if n <= 1:
        lines.append("Se ha detectado **una única revisión** guardada. No hay indicios de "
                     "actualizaciones incrementales posteriores.")
    else:
        lines.append(f"⚠️ **Se han detectado {n} revisiones guardadas en el mismo archivo.** "
                     f"Esto significa que el documento se guardó (o se firmó/anotó) más de una vez, "
                     f"y el contenido de las versiones anteriores puede seguir físicamente presente "
                     f"en el archivo, aunque un lector normal solo muestre la última.\n")
        for r in rev_info["revisiones"]:
            lines.append(f"**Revisión {r['revision']}** — contenido visible en ese momento:")
            lines.append(f"> {r['texto_visible'][:500]}\n")
        if rev_info["contenido_difiere_entre_revisiones"]:
            lines.append("🔴 **El contenido visible difiere entre revisiones.** Esto es un indicio "
                         "relevante: el documento fue editado después de un guardado anterior y esa "
                         "versión previa sigue siendo recuperable.\n")

    lines.append("## 2. Redacciones defectuosas\n")
    if not redacciones:
        lines.append("No se ha encontrado texto extraíble bajo ningún rectángulo opaco.")
    else:
        lines.append(f"🔴 **Se han encontrado {len(redacciones)} posible(s) redacción(es) defectuosa(s):** "
                     "texto que sigue siendo extraíble por debajo de un rectángulo relleno "
                     "(típicamente usado para \"tachar\" información sensible).\n")
        for h in redacciones:
            lines.append(f"- Página {h['pagina']}, posición {h['posicion']} — "
                         f"texto oculto recuperado: **\"{h['texto_oculto_recuperado']}\"**")

    lines.append("\n## 3. Metadatos\n")
    if meta:
        lines.append("| Campo | Valor |")
        lines.append("|---|---|")
        for k, v in meta.items():
            lines.append(f"| {k} | {v} |")
    else:
        lines.append("El documento no contiene metadatos.")

    lines.append("\n## 4. Firma digital\n")
    if tiene_firma:
        lines.append("El documento **contiene un campo de firma digital**.")
        if rev_info["num_revisiones"] > 1:
            lines.append("⚠️ Además tiene múltiples revisiones — comprueba si la firma cubre "
                         "el contenido visible actual o solo una revisión anterior (ataque conocido "
                         "como *incremental update / shadow attack* sobre PDFs firmados).")
    else:
        lines.append("No se ha detectado firma digital en el documento.")

    lines.append("\n## ⚖️ Límites de este análisis\n")
    lines.append("- Esta herramienta identifica **indicios técnicos**, no constituye una prueba "
                 "pericial ni una certificación de autenticidad por sí sola.")
    lines.append("- La detección de redacciones se basa en superposición geométrica de texto y "
                 "rectángulos; PDFs generados de formas no estándar pueden no ser detectados "
                 "(falsos negativos) y disposiciones visuales complejas pueden generar falsos positivos.")
    lines.append("- Para que este análisis tenga validez en un procedimiento judicial debe "
                 "incorporarse a un informe pericial completo, firmado por un perito.")

    lines.append("\n---\n")
    lines.append(f"*Generado con pdf_forense.py v{VERSION} — "
                 "https://github.com/AnkbNikas/pdf-forense*")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        prog="pdf_forense",
        description="Audita un PDF en busca de revisiones ocultas y redacciones defectuosas."
    )
    parser.add_argument("pdf", help="Ruta al PDF a analizar")
    parser.add_argument("--salida", default="informe_pdf", help="Nombre base de los ficheros de salida")
    args = parser.parse_args()

    path = Path(args.pdf)
    if not path.exists():
        print(f"Error: el archivo '{args.pdf}' no existe.", file=sys.stderr)
        sys.exit(1)

    print(f"[*] Analizando: {path}")
    print("[*] Buscando revisiones ocultas...")
    rev_info = analizar_revisiones(path, path.parent)

    print("[*] Buscando redacciones defectuosas...")
    redacciones = analizar_redacciones(path)

    print("[*] Extrayendo metadatos y firma...")
    meta, tiene_firma = analizar_metadatos_y_firma(path)

    informe = construir_informe(path, rev_info, redacciones, meta, tiene_firma)

    md_path = Path(f"{args.salida}.md")
    md_path.write_text(informe, encoding="utf-8")

    json_path = Path(f"{args.salida}.json")
    json_path.write_text(json.dumps({
        "archivo": str(path),
        "revisiones": rev_info,
        "redacciones_defectuosas": redacciones,
        "metadatos": meta,
        "tiene_firma_digital": tiene_firma,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[+] Informe Markdown: {md_path}")
    print(f"[+] Datos JSON:       {json_path}")
    if rev_info["num_revisiones"] > 1:
        print(f"[!] {rev_info['num_revisiones']} revisiones detectadas — revisa el informe.")
    if redacciones:
        print(f"[!] {len(redacciones)} posible(s) redacción(es) defectuosa(s) — revisa el informe.")


if __name__ == "__main__":
    main()
