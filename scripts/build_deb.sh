#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

PLUGIN_SLUG="kalinka-plugin-qobuz"

# Avoid stale wheels and generated metadata from previous runs.
rm -rf pkgroot/ dist/
mkdir -p pkgroot/opt/kalinka/wheels
mkdir -p pkgroot/DEBIAN

./scripts/build_wheel.sh

WHEEL_PATH=$(find dist -maxdepth 1 -type f -name 'kalinka_plugin_qobuz-*-py3-none-any.whl' | sort | head -n 1)
if [ -z "${WHEEL_PATH}" ] || [ ! -f "${WHEEL_PATH}" ]; then
    echo "Error: No matching wheel found in dist/." >&2
    exit 1
fi

WHEEL_FILE=$(basename "${WHEEL_PATH}")
VERSION="${WHEEL_FILE#kalinka_plugin_qobuz-}"
VERSION="${VERSION%-py3-none-any.whl}"

cp "${WHEEL_PATH}" pkgroot/opt/kalinka/wheels/

# A script, not `-m`, so the build needs the plugin SDK alone, none of the
# plugin's runtime deps.
mkdir -p pkgroot/opt/kalinka/allowed_packages
python3 src/kalinka_plugin_qobuz/optional_packages.py \
    > pkgroot/opt/kalinka/allowed_packages/qobuz.json

sed "s|@VERSION@|${VERSION}|g" debian/control.in > pkgroot/DEBIAN/control

cp debian/triggers pkgroot/DEBIAN/triggers
cp debian/prerm pkgroot/DEBIAN/prerm

chmod 755 pkgroot/DEBIAN/prerm

dpkg-deb --root-owner-group --build pkgroot "${PLUGIN_SLUG}_${VERSION}_all.deb"