"""Punto de entrada de la app de escritorio: servidor local + ventana nativa (WebKit en macOS, WebView2 en Windows)."""
import multiprocessing
import os
import sys
from pathlib import Path


def data_dir():
    """Carpeta de datos de la app (opciones guardadas y registro)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "Anonimizador"
    return Path.home() / "Library" / "Application Support" / "Anonimizador"


def log_path():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "Anonimizador.log"
    return data_dir() / "Anonimizador.log"


class Api:
    """Funciones nativas accesibles desde la página (window.pywebview.api)."""

    def __init__(self):
        self.window = None

    def pick_folder(self):
        import webview
        result = self.window.create_file_dialog(webview.FileDialog.FOLDER)
        return result[0] if result else None

    def open_url(self, url):
        import webbrowser
        if url.startswith(("https://", "http://", "mailto:")):
            webbrowser.open(url)


def main():
    multiprocessing.freeze_support()

    # Anonimizador --selftest informe.txt: comprobación sin ventana (la usa la integración continua)
    if "--selftest" in sys.argv:
        import anonimizador
        report = sys.argv[sys.argv.index("--selftest") + 1]
        sys.exit(anonimizador.selftest(report))

    data_dir().mkdir(parents=True, exist_ok=True)
    if getattr(sys, "frozen", False):
        # en una app de escritorio no hay terminal: el registro va a un archivo
        log_path().parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(log_path(), "a", buffering=1, encoding="utf-8")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    import webview
    import anonimizador

    anonimizador.state["app"] = True
    server = anonimizador.start_server()
    port = server.server_port

    api = Api()
    api.window = webview.create_window(
        "Anonimizador", f"http://127.0.0.1:{port}/", js_api=api,
        width=980, height=900, min_size=(640, 600),
    )
    # private_mode=False para que las opciones guardadas se conserven entre usos
    webview.start(private_mode=False, storage_path=str(data_dir()))
    server.shutdown()


if __name__ == "__main__":
    main()
