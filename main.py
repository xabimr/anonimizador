"""Punto de entrada de «Anonimizador.app»: servidor local + ventana nativa (WebKit)."""
import os
import sys
from pathlib import Path


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
    if getattr(sys, "frozen", False):
        # en una app de Mac no hay terminal: el registro va a ~/Library/Logs
        log_dir = Path.home() / "Library" / "Logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(log_dir / "Anonimizador.log", "a", buffering=1, encoding="utf-8")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    import webview
    import anonimizador

    anonimizador.state["app"] = True
    server = anonimizador.start_server()
    port = server.server_port

    api = Api()
    storage = Path.home() / "Library" / "Application Support" / "Anonimizador"
    storage.mkdir(parents=True, exist_ok=True)
    api.window = webview.create_window(
        "Anonimizador", f"http://127.0.0.1:{port}/", js_api=api,
        width=980, height=900, min_size=(640, 600),
    )
    # private_mode=False para que las opciones guardadas se conserven entre usos
    webview.start(private_mode=False, storage_path=str(storage))
    server.shutdown()


if __name__ == "__main__":
    main()
