# -*- mode: python ; coding: utf-8 -*-
# Especificación de PyInstaller para la versión de Windows. La usa .github/workflows/windows.yml.
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

SRC = os.environ["ANON_SRC"]

datas = [(os.path.join(SRC, "index.html"), ".")]
binaries, hidden = [], []
for pkg in ["presidio_analyzer", "spacy", "thinc", "es_core_news_lg", "pymupdf", "pymupdf4llm",
            "webview", "clr_loader", "pythonnet", "tldextract", "phonenumbers", "spacy_legacy",
            "spacy_loggers", "srsly", "cymem", "preshed", "blis", "murmurhash", "catalogue",
            "confection", "weasel", "langcodes"]:
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue
    datas += d
    binaries += b
    hidden += h
# spaCy y Presidio comprueban los modelos instalados por sus metadatos
for pkg in ["es_core_news_lg", "spacy", "presidio_analyzer", "thinc"]:
    datas += copy_metadata(pkg)
hidden += collect_submodules("spacy.lang.es") + ["anonimizador", "clr"]

a = Analysis(
    [os.path.join(SRC, "main.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["torch", "transformers", "tkinter", "matplotlib", "IPython",
              "pandas", "scipy", "stanza", "flair", "azure"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Anonimizador",
          console=False, icon=os.path.join(SRC, "icono.png"))
coll = COLLECT(exe, a.binaries, a.datas, name="Anonimizador")
