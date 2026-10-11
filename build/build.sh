#!/bin/zsh
# Compila «Anonimizador.app» (autocontenida: Python, Presidio, spaCy y el modelo de español) y su .dmg.
#
#   ./build/build.sh
#
# Se compila en ~/Library/Caches/anonimizador-build y las dependencias se instalan en la variante
# para macOS 14, para que la app funcione en cualquier Mac con Apple Silicon y macOS 14 o posterior.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$SRC/dist}"   # dónde se deja la .dmg (se puede cambiar con OUT_DIR=…)
BUILD="$HOME/Library/Caches/anonimizador-build"
VERSION="${VERSION:-1.0}"

mkdir -p "$BUILD" "$OUT_DIR"
cd "$BUILD"

echo "» Entorno de compilación (dependencias para macOS 14+)"
[ -x venv/bin/python ] || uv venv -q --python 3.12 venv
(cd "$SRC" && uv lock -q && uv export -q --frozen --no-hashes --no-emit-project) > requirements.txt
MACOSX_DEPLOYMENT_TARGET=14.0 uv pip install -q --python venv/bin/python \
  --python-platform aarch64-apple-darwin -r requirements.txt

echo "» Icono"
rm -rf icon.iconset && mkdir icon.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s "$SRC/icono.png" --out icon.iconset/icon_${s}x${s}.png >/dev/null
  sips -z $((s*2)) $((s*2)) "$SRC/icono.png" --out icon.iconset/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns icon.iconset -o Anonimizador.icns

echo "» PyInstaller"
ANON_SRC="$SRC" ANON_ICON="$BUILD/Anonimizador.icns" ANON_VERSION="$VERSION" \
  venv/bin/pyinstaller -y --log-level WARN --distpath dist --workpath work "$SRC/build/Anonimizador.spec"

echo "» Firma ad hoc"
codesign --force --deep --sign - "dist/Anonimizador.app"

echo "» DMG"
rm -rf dmg && mkdir dmg && cp -R "dist/Anonimizador.app" dmg/ && ln -s /Applications dmg/Aplicaciones
rm -f "$OUT_DIR/Anonimizador.dmg"
hdiutil create -quiet -volname "Anonimizador" -srcfolder dmg -fs APFS -format ULMO "$OUT_DIR/Anonimizador.dmg"

du -sh "dist/Anonimizador.app" "$OUT_DIR/Anonimizador.dmg"
echo "Hecho: $OUT_DIR/Anonimizador.dmg"
