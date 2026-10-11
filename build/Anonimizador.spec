# -*- mode: python ; coding: utf-8 -*-
# Especificación de PyInstaller para «Anonimizador.app». Se lanza desde build/build.sh.
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

SRC = os.environ["ANON_SRC"]
VERSION = os.environ.get("ANON_VERSION", "1.0")

datas = [(os.path.join(SRC, "index.html"), ".")]
binaries, hidden = [], []
for pkg in ["presidio_analyzer", "spacy", "thinc", "es_core_news_lg", "pymupdf", "pymupdf4llm",
            "webview", "tldextract", "phonenumbers", "spacy_legacy", "spacy_loggers", "srsly",
            "cymem", "preshed", "blis", "murmurhash", "catalogue", "confection", "weasel", "langcodes"]:
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
hidden += collect_submodules("spacy.lang.es") + ["anonimizador"]

a = Analysis(
    [os.path.join(SRC, "main.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["torch", "transformers", "tkinter", "matplotlib", "IPython", "PyObjCTest",
              "pandas", "scipy", "stanza", "flair", "azure"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Anonimizador",
          console=False, target_arch="arm64", codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, name="Anonimizador")
app = BUNDLE(
    coll,
    name="Anonimizador.app",
    icon=os.environ.get("ANON_ICON") or None,
    bundle_identifier="gal.uvigo.anonimizador",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "Anonimizador",
        "CFBundleShortVersionString": VERSION,
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.productivity",
    },
)
