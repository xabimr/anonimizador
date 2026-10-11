#!/usr/bin/env python3
"""Anonimizador local: interfaz web sobre Presidio para .docx, .pdf, .txt y .md.

Arranca un servidor en http://127.0.0.1:8765 y abre el navegador.
Nada sale del ordenador: el análisis se hace con spaCy (es_core_news_lg) en local.
"""

import html
import json
import os
import re
import socket
import subprocess
import sys
import threading
import traceback
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HOST, PORT = "127.0.0.1", 8765
# en la app empaquetada los recursos están en sys._MEIPASS
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

# Etiquetas que aparecen en el texto anonimizado
LABELS = {
    "PERSON": "PERSONA",
    "LOCATION": "LUGAR",
    "ORGANIZATION": "ORGANIZACION",
    "EMAIL": "EMAIL",
    "PHONE": "TELEFONO",
    "DNI": "DNI",
    "IBAN": "IBAN",
    "ORCID": "ORCID",
    "URL": "URL",
    "CUSTOM": "ANONIMIZADO",
}

PATTERNS = {
    "EMAIL": r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b",
    "PHONE": r"(?<![\w+])(?:\+34[\s.-]?)?[6789]\d{2}(?:[\s.-]?\d{2,3}){2,3}(?!\d)",
    "DNI": r"\b(?:\d{8}|[XYZ][\s-]?\d{7})[\s-]?[A-HJ-NP-TV-Z]\b",
    "IBAN": r"\b[A-Z]{2}\d{2}(?:\s?[\dA-Z]{4}){4,7}(?:\s?[\dA-Z]{1,4})?\b",
    "ORCID": r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b",
    "URL": r"\bhttps?://[^\s<>\"]+|\bwww\.[^\s<>\"]+",
    # spaCy detecta mal las instituciones académicas, que son lo que más delata la autoría
    "ORGANIZATION": r"\b(?:Universi(?:dad|dade|tat|ty)|Facult(?:ad|ade|y)|Faculdade|Institut[oe]?|"
                    r"Escuela|Escola|Fundaci[óo]n)"
                    r"(?:[ \t]+(?:(?:de|da|do|del|das|dos|of|for|la|les|y|e|i|d')[ \t]+)?[A-ZÁÉÍÓÚÑ][\w'’-]+)+",
}

state = {"analyzer": None, "error": None, "app": False}
batches = {}  # batch id -> {texto normalizado: marcador}, para numerar igual en todo el lote
batches_lock = threading.Lock()


# ---------------------------------------------------------------- análisis

def load_analyzer():
    try:
        from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerRegistry
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_analyzer.predefined_recognizers import SpacyRecognizer

        conf = {
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "es", "model_name": "es_core_news_lg"}],
            "ner_model_configuration": {
                "model_to_presidio_entity_mapping": {
                    "PER": "PERSON", "LOC": "LOCATION", "ORG": "ORGANIZATION",
                },
                "labels_to_ignore": ["MISC"],
            },
        }
        nlp = NlpEngineProvider(nlp_configuration=conf).create_engine()
        registry = RecognizerRegistry(supported_languages=["es"])
        registry.add_recognizer(SpacyRecognizer(
            supported_language="es",
            supported_entities=["PERSON", "LOCATION", "ORGANIZATION"],
        ))
        for entity, regex in PATTERNS.items():
            registry.add_recognizer(PatternRecognizer(
                supported_entity=entity, supported_language="es",
                patterns=[Pattern(entity.lower(), regex, 0.9)],
            ))
        state["analyzer"] = AnalyzerEngine(registry=registry, nlp_engine=nlp, supported_languages=["es"])
    except Exception:
        state["error"] = traceback.format_exc()


def find_entities(text, opts):
    """Devuelve [(inicio, fin, tipo)] sin solapamientos, ordenado."""
    found = []
    entities = [e for e in opts["entities"] if e in LABELS and e != "CUSTOM"]
    if entities and text.strip():
        for r in state["analyzer"].analyze(text=text, language="es", entities=entities,
                                           score_threshold=opts["threshold"]):
            found.append((r.start, r.end, r.entity_type, r.score))
    for term in opts["extra"]:
        for m in re.finditer(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.IGNORECASE):
            found.append((m.start(), m.end(), "CUSTOM", 2.0))

    allow = {a.casefold() for a in opts["allow"]}
    cleaned = []
    for s, e, t, sc in found:
        frag = text[s:e]
        # spaCy a veces une líneas distintas en una entidad: se queda solo la primera
        if "\n" in frag.strip():
            lead = len(frag) - len(frag.lstrip())
            cut = frag.find("\n", lead)
            e = s + cut; frag = text[s:e]
        # recorta espacios y puntuación que spaCy a veces incluye en los bordes
        while s < e and not frag[0].isalnum():
            s += 1; frag = frag[1:]
        while e > s and not frag[-1].isalnum() and frag[-1] not in ")/":
            e -= 1; frag = frag[:-1]
        if e - s < 2 or frag.casefold() in allow:
            continue
        cleaned.append((s, e, t, sc))

    # en solapamientos gana el más largo (tapa más) y, a igualdad, el de mayor puntuación
    cleaned.sort(key=lambda x: (-(x[1] - x[0]), -x[3]))
    taken = []
    for s, e, t, sc in cleaned:
        if all(e <= ts or s >= te for ts, te, _ in taken):
            taken.append((s, e, t))
    return sorted(taken)


def placeholder(batch, ent_type, frag):
    key = re.sub(r"\s+", " ", frag).strip().casefold()
    with batches_lock:
        mapping = batches.setdefault(batch, {})
        if key not in mapping:
            label = LABELS[ent_type]
            n = sum(1 for v in mapping.values() if v.startswith(f"[{label}_")) + 1
            mapping[key] = f"[{label}_{n}]"
        return mapping[key]


def build_spans(text, opts, batch):
    spans, report = [], {}
    for s, e, t in find_entities(text, opts):
        frag = text[s:e]
        ph = placeholder(batch, t, frag)
        spans.append((s, e, ph))
        report.setdefault(ph, set()).add(re.sub(r"\s+", " ", frag))
    return spans, report


def replace_text(text, spans):
    out, pos = [], 0
    for s, e, ph in spans:
        out.append(text[pos:s]); out.append(ph); pos = e
    out.append(text[pos:])
    return "".join(out)


# ---------------------------------------------------------------- formatos

def process_text(data, opts, batch):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    spans, report = build_spans(text, opts, batch)
    return replace_text(text, spans).encode("utf-8"), report, []


W_T = re.compile(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.S)
W_T_EMPTY = re.compile(r"<w:t(?:\s[^>]*)?/>")
DOCX_PARTS = re.compile(r"word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$")


def anonymize_docx_xml(xml, opts, batch, report):
    """Anonimiza el texto de los <w:t> conservando el formato de cada fragmento."""
    para_ends = [m.end() for m in re.finditer(r"</w:p>", xml)]
    matches = list(W_T.finditer(xml))
    if not matches:
        return xml
    segs = [html.unescape(m.group(2)) for m in matches]
    # texto global: los fragmentos de un párrafo van seguidos; entre párrafos, salto de línea
    starts, parts, pos, p_idx = [], [], 0, 0
    for m, seg in zip(matches, segs):
        new_para = False
        while p_idx < len(para_ends) and para_ends[p_idx] <= m.start():
            p_idx += 1; new_para = True
        if new_para and parts:
            parts.append("\n\n"); pos += 2
        starts.append(pos); parts.append(seg); pos += len(seg)
    text = "".join(parts)

    spans, rep = build_spans(text, opts, batch)
    for k, v in rep.items():
        report.setdefault(k, set()).update(v)
    if not spans:
        return xml

    new_segs, si = [], 0
    for seg, g0 in zip(segs, starts):
        buf = []
        for k, ch in enumerate(seg):
            g = g0 + k
            while si < len(spans) and spans[si][1] <= g:
                si += 1
            if si < len(spans) and spans[si][0] <= g < spans[si][1]:
                if g == spans[si][0]:  # el marcador va en el fragmento donde empieza la entidad
                    buf.append(spans[si][2])
                continue
            buf.append(ch)
        new_segs.append("".join(buf))

    out, last = [], 0
    for m, old, new in zip(matches, segs, new_segs):
        out.append(xml[last:m.start()])
        if new == old:
            out.append(m.group(0))
        else:
            open_tag = m.group(1)
            if "xml:space" not in open_tag:
                open_tag = open_tag[:-1] + ' xml:space="preserve">'
            out.append(open_tag + html.escape(new, quote=False) + m.group(3))
        last = m.end()
    out.append(xml[last:])
    return "".join(out)


def clean_core(xml):
    for tag in ("dc:creator", "cp:lastModifiedBy", "dc:title", "dc:subject", "cp:keywords",
                "dc:description", "cp:category", "cp:contentStatus"):
        xml = re.sub(rf"<{tag}>.*?</{tag}>", f"<{tag}></{tag}>", xml, flags=re.S)
        xml = re.sub(rf"<{tag}/>", f"<{tag}></{tag}>", xml)
    return xml


def process_docx(data, opts, batch):
    report, notes = {}, []
    zin = zipfile.ZipFile(BytesIO(data))
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            content = zin.read(item.filename)
            name = item.filename
            if DOCX_PARTS.match(name):
                xml = content.decode("utf-8")
                xml = anonymize_docx_xml(xml, opts, batch, report)
                if opts["metadata"]:
                    xml = re.sub(r'w:author="[^"]*"', 'w:author="Autor"', xml)
                    xml = re.sub(r'w:initials="[^"]*"', 'w:initials="A"', xml)
                content = xml.encode("utf-8")
            elif re.match(r"word/_rels/.*\.rels$", name):
                xml = content.decode("utf-8")
                if "EMAIL" in opts["entities"]:
                    xml = re.sub(r'Target="mailto:[^"]*"', 'Target="mailto:anonimizado"', xml)
                content = xml.encode("utf-8")
            elif opts["metadata"] and name == "docProps/core.xml":
                content = clean_core(content.decode("utf-8")).encode("utf-8")
            elif opts["metadata"] and name == "docProps/app.xml":
                xml = content.decode("utf-8")
                xml = re.sub(r"<(Company|Manager)>.*?</\1>", r"<\1></\1>", xml, flags=re.S)
                content = xml.encode("utf-8")
            elif opts["metadata"] and name == "word/people.xml":
                xml = content.decode("utf-8")
                xml = re.sub(r'w15:author="[^"]*"', 'w15:author="Autor"', xml)
                xml = re.sub(r'w15:userId="[^"]*"', 'w15:userId="Autor"', xml)
                content = xml.encode("utf-8")
            zout.writestr(item, content)
    if opts["metadata"]:
        notes.append("Metadatos limpiados (autor, empresa, autores de comentarios y cambios).")
    return buf.getvalue(), report, notes


def process_pdf(data, opts, batch):
    import pymupdf

    report, notes = {}, []
    doc = pymupdf.open(stream=data, filetype="pdf")
    pages_without_text = 0
    missed = set()
    for page in doc:
        text = page.get_text()
        if not text.strip():
            pages_without_text += 1
            continue
        spans, rep = build_spans(text, opts, batch)
        for k, v in rep.items():
            report.setdefault(k, set()).update(v)
        for s, e, ph in spans:
            frag = text[s:e]
            rects = page.search_for(frag) if "\n" not in frag else []
            if not rects:  # la entidad cruza un salto de línea: se busca palabra a palabra
                for word in re.split(r"\s+", frag):
                    if len(word) >= 3:
                        rects += page.search_for(word)
            if not rects:
                missed.add(frag)
            for r in rects:
                page.add_redact_annot(r, fill=(0, 0, 0))
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
    if opts["metadata"]:
        doc.set_metadata({})
        doc.del_xml_metadata()
        notes.append("Metadatos del PDF eliminados.")
    if pages_without_text:
        notes.append(f"{pages_without_text} página(s) sin texto (¿escaneadas?): no se han podido anonimizar.")
    if missed:
        notes.append("No se localizaron en la página (revísalo a mano): " + ", ".join(sorted(missed)))
    notes.append("En el PDF los datos se tapan con recuadros negros; la tabla de abajo indica qué había.")
    out = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    return out, report, notes


PROCESSORS = {".txt": process_text, ".md": process_text, ".docx": process_docx, ".pdf": process_pdf}


# ---------------------------------------------------------------- salida en Markdown

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_list_formats(z):
    """numId -> {nivel: formato} a partir de numbering.xml (para distinguir viñetas y numeración)."""
    import xml.etree.ElementTree as ET
    if "word/numbering.xml" not in z.namelist():
        return {}
    root = ET.fromstring(z.read("word/numbering.xml"))
    abstract = {}
    for an in root.iter(W + "abstractNum"):
        levels = {}
        for lvl in an.iter(W + "lvl"):
            fmt = lvl.find(W + "numFmt")
            levels[lvl.get(W + "ilvl")] = fmt.get(W + "val") if fmt is not None else "bullet"
        abstract[an.get(W + "abstractNumId")] = levels
    result = {}
    for num in root.iter(W + "num"):
        ref = num.find(W + "abstractNumId")
        if ref is not None:
            result[num.get(W + "numId")] = abstract.get(ref.get(W + "val"), {})
    return result


def _md_escape(text):
    # sin tocar [ ] ni _ para que los marcadores [PERSONA_1] queden legibles
    return re.sub(r"([*`<>])", r"\\\1", text)


def _docx_runs_md(p):
    """Texto de un párrafo con negrita y cursiva, uniendo fragmentos con el mismo formato."""
    pieces = []  # [(negrita, cursiva, texto)]
    for r in p.iter(W + "r"):
        rpr = r.find(W + "rPr")
        def on(tag):
            el = rpr.find(W + tag) if rpr is not None else None
            return el is not None and el.get(W + "val") not in ("0", "false")
        b, i = on("b"), on("i")
        for child in r:
            if child.tag == W + "t":
                pieces.append((b, i, _md_escape(child.text or "")))
            elif child.tag == W + "tab":
                pieces.append((b, i, " "))
            elif child.tag in (W + "br", W + "cr"):
                pieces.append((False, False, "  \n"))
            elif child.tag == W + "footnoteReference":
                pieces.append((False, False, f"[^{child.get(W + 'id')}]"))
    merged = []
    for b, i, t in pieces:
        if merged and merged[-1][0] == b and merged[-1][1] == i:
            merged[-1][2] += t
        else:
            merged.append([b, i, t])
    out = []
    for b, i, t in merged:
        core = t.strip()
        if (b or i) and core:  # los espacios quedan fuera de las marcas para que Markdown las reconozca
            mark = "***" if b and i else "**" if b else "*"
            lead, trail = t[:len(t) - len(t.lstrip())], t[len(t.rstrip()):]
            out.append(f"{lead}{mark}{core}{mark}{trail}")
        else:
            out.append(t)
    return "".join(out).strip()


def _docx_paragraph_md(p, lists):
    text = _docx_runs_md(p)
    ppr = p.find(W + "pPr")
    style = ""
    if ppr is not None and ppr.find(W + "pStyle") is not None:
        style = ppr.find(W + "pStyle").get(W + "val", "")
    m = re.match(r"(?i)(?:heading|t[ií]tulo|encabezado)\s*(\d)", style)
    if text and m:
        return "#" * min(int(m.group(1)), 6) + " " + text
    if text and re.fullmatch(r"(?i)title|t[ií]tulo", style):
        return "# " + text
    numpr = ppr.find(W + "numPr") if ppr is not None else None
    if text and numpr is not None:
        ilvl_el, numid_el = numpr.find(W + "ilvl"), numpr.find(W + "numId")
        ilvl = ilvl_el.get(W + "val") if ilvl_el is not None else "0"
        numid = numid_el.get(W + "val") if numid_el is not None else ""
        fmt = lists.get(numid, {}).get(ilvl, "bullet")
        bullet = "-" if fmt in ("bullet", "none") else "1."
        return "    " * int(ilvl or 0) + f"{bullet} {text}"
    return text


def _docx_table_md(tbl, lists):
    rows = []
    for tr in tbl.findall(W + "tr"):
        cells = []
        for tc in tr.findall(W + "tc"):
            paras = [_docx_paragraph_md(p, lists) for p in tc.iter(W + "p")]
            cells.append("<br>".join(t for t in paras if t).replace("|", "\\|"))
        rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _docx_blocks_md(parent, lists):
    blocks = []
    for el in parent:
        if el.tag == W + "p":
            blocks.append(_docx_paragraph_md(el, lists))
        elif el.tag == W + "tbl":
            blocks.append(_docx_table_md(el, lists))
        elif el.tag in (W + "sdt", W + "sdtContent", W + "customXml", W + "ins"):
            blocks.extend(_docx_blocks_md(el, lists))
    return blocks


def docx_to_markdown(data):
    import xml.etree.ElementTree as ET
    z = zipfile.ZipFile(BytesIO(data))
    lists = _docx_list_formats(z)
    body = ET.fromstring(z.read("word/document.xml")).find(W + "body")
    blocks = _docx_blocks_md(body, lists)
    # párrafos de lista seguidos van juntos; el resto separado por línea en blanco
    out = []
    for b in blocks:
        if not b:
            continue
        is_item = re.match(r"\s*(?:-|1\.) ", b)
        if out and is_item and out[-1][1]:
            out[-1][0] += "\n" + b
        else:
            out.append([b, bool(is_item)])
    md = "\n\n".join(b for b, _ in out)
    if "word/footnotes.xml" in z.namelist():
        notes = []
        for fn in ET.fromstring(z.read("word/footnotes.xml")).iter(W + "footnote"):
            fid = fn.get(W + "id")
            if fid and int(fid) > 0:
                text = " ".join(t for t in (_docx_runs_md(p) for p in fn.iter(W + "p")) if t)
                text = re.sub(r"^\[\^\d+\]\s*", "", text)
                notes.append(f"[^{fid}]: {text}")
        if notes:
            md += "\n\n" + "\n".join(notes)
    return md + "\n"


def to_markdown(ext, data, opts, batch):
    """Anonimiza y devuelve Markdown. En Word se anonimiza antes de convertir, para no partir nombres."""
    if ext == ".docx":
        anon, report, notes = process_docx(data, opts, batch)
        notes = [n for n in notes if not n.startswith("Metadatos")]
        return docx_to_markdown(anon).encode("utf-8"), report, notes
    if ext == ".pdf":
        import pymupdf
        import pymupdf4llm
        doc = pymupdf.open(stream=data, filetype="pdf")
        empty = sum(1 for page in doc if not page.get_text().strip())
        md = pymupdf4llm.to_markdown(doc, show_progress=False)
        doc.close()
        spans, report = build_spans(md, opts, batch)
        notes = [f"{empty} página(s) sin texto (¿escaneadas?): no se han podido convertir."] if empty else []
        return replace_text(md, spans).encode("utf-8"), report, notes
    return process_text(data, opts, batch)


def unique_path(folder, name, ext=None):
    stem, orig_ext = os.path.splitext(name)
    ext = ext or orig_ext
    path = Path(folder) / f"{stem}_anonimizado{ext}"
    n = 2
    while path.exists():
        path = Path(folder) / f"{stem}_anonimizado_{n}{ext}"
        n += 1
    return path


# ---------------------------------------------------------------- sistema

def pick_folder_dialog():
    title = "Carpeta de salida para los documentos anonimizados"
    if sys.platform == "darwin":
        script = f'POSIX path of (choose folder with prompt "{title}")'
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    elif sys.platform == "win32":
        ps = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
              f"$d.Description = '{title}';"
              "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }")
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, text=True)
    else:
        return None
    return r.stdout.strip() or None


def reveal(path):
    """Muestra el archivo en el Finder o en el Explorador."""
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path])
    elif sys.platform == "win32":
        subprocess.run(["explorer", "/select,", os.path.normpath(path)])
    else:
        subprocess.run(["xdg-open", os.path.dirname(path)])


def selftest(report_path):
    """Comprobación sin ventana para la integración continua: carga el modelo y anonimiza un texto."""
    load_analyzer()
    ok, lines = False, []
    if state["error"]:
        lines.append(state["error"])
    else:
        opts = {"entities": list(LABELS), "extra": [], "allow": [], "threshold": 0.4, "metadata": True}
        text = ("La investigadora María López García, de la Universidad de Salamanca, "
                "escribe desde maria.lopez@usal.es o el 600 123 456.")
        out, report, _ = process_text(text.encode("utf-8"), opts, "selftest")
        result = out.decode("utf-8")
        lines.append(result)
        ok = all(x not in result for x in ("María López", "Universidad de Salamanca", "maria.lopez@usal.es", "600 123 456"))
    lines.append("SELFTEST OK" if ok else "SELFTEST FALLO")
    Path(report_path).write_text("\n".join(lines), encoding="utf-8")
    return 0 if ok else 1


# ---------------------------------------------------------------- servidor

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = (BASE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            self.send_json({"ready": state["analyzer"] is not None, "error": state["error"], "app": state["app"],
                            "platform": sys.platform})
        else:
            self.send_error(404)

    def do_POST(self):
        origin = self.headers.get("Origin")
        port = self.server.server_port
        if origin and origin not in (f"http://{HOST}:{port}", f"http://localhost:{port}"):
            return self.send_json({"error": "Origen no permitido"}, 403)
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        if url.path == "/api/pick-folder":  # solo en modo navegador; en la app se usa el diálogo de la ventana
            return self.send_json({"path": pick_folder_dialog()})

        if url.path == "/api/reveal":
            reveal(q.get("path", ""))
            return self.send_json({"ok": True})

        if url.path == "/api/quit":
            self.send_json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        if url.path == "/api/anonymize":
            return self.anonymize(q, body)

        self.send_error(404)

    def anonymize(self, q, data):
        if state["analyzer"] is None:
            return self.send_json({"error": "El modelo aún se está cargando"}, 503)
        name = os.path.basename(q.get("name", "documento.txt"))
        ext = os.path.splitext(name)[1].lower()
        out_dir = os.path.expanduser(q.get("out", "").strip())
        if ext not in PROCESSORS:
            return self.send_json({"error": f"Formato no admitido: {ext or 'sin extensión'}"})
        if not out_dir or not os.path.isdir(out_dir):
            return self.send_json({"error": "La carpeta de salida no existe"})
        try:
            opts_raw = json.loads(unquote(self.headers.get("X-Options") or "{}"))
        except json.JSONDecodeError:
            opts_raw = {}
        opts = {
            "entities": opts_raw.get("entities") or list(LABELS),
            "extra": [t.strip() for t in opts_raw.get("extra", []) if t.strip()],
            "allow": [t.strip() for t in opts_raw.get("allow", []) if t.strip()],
            "threshold": float(opts_raw.get("threshold", 0.4)),
            "metadata": bool(opts_raw.get("metadata", True)),
        }
        markdown = opts_raw.get("format") == "markdown"
        try:
            batch = q.get("batch", "default")
            if markdown:
                out_bytes, report, notes = to_markdown(ext, data, opts, batch)
            else:
                out_bytes, report, notes = PROCESSORS[ext](data, opts, batch)
            path = unique_path(out_dir, name, ".md" if markdown else None)
            path.write_bytes(out_bytes)
        except Exception as exc:
            traceback.print_exc()
            return self.send_json({"error": f"No se pudo procesar: {exc}"})
        found = sorted(({"placeholder": k, "texts": sorted(v)} for k, v in report.items()),
                       key=lambda x: x["placeholder"])
        self.send_json({"output": str(path), "found": found, "notes": notes})


def start_server(port=PORT):
    """Arranca el servidor en segundo plano; si el puerto está ocupado usa uno libre."""
    threading.Thread(target=load_analyzer, daemon=True).start()
    try:
        server = ThreadingHTTPServer((HOST, port), Handler)
    except OSError:
        server = ThreadingHTTPServer((HOST, 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    url = f"http://{HOST}:{PORT}/"
    with socket.socket() as s:
        if s.connect_ex((HOST, PORT)) == 0:  # ya está abierto: solo se abre el navegador
            webbrowser.open(url)
            return
    threading.Thread(target=load_analyzer, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, webbrowser.open, args=[url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
