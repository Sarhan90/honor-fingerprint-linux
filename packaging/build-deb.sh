#!/bin/bash
# Собирает два пакета (root не нужен):
#   libfprint-egis-sdcp_<версия>_amd64.deb — драйвер: libfprint с SDCP и поддержкой Egis 1c7a:05aa
#   fprint-manager_<версия>_all.deb       — приложение «Отпечатки пальцев»
#
#   ./build-deb.sh [версия-драйвера] [версия-приложения]
set -euo pipefail
umask 022

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/driver"
GUI="$ROOT/app"
OUT_DIR="$ROOT/dist"
DRV_VERSION="${1:-1.94.9+egis05aa.1}"
APP_VERSION="${2:-1.0.1}"
MAINTAINER="Sarhan90 <Sarhan90@users.noreply.github.com>"

DRV=libfprint-egis-sdcp
DRV_OPT=/opt/$DRV
APP=fprint-manager
APP_OPT=/opt/$APP

mkdir -p "$OUT_DIR"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ================================================================ драйвер
echo "==> [$DRV] сборка libfprint"
DRVROOT="$WORK/drv"
BUILD="$WORK/build"
# Копия исходников со свежими временами файлов: meson не любит «файлы из будущего»
mkdir -p "$WORK/src"
tar -C "$SRC" --exclude=./build --exclude=./setup.log -cf - . | tar -xmf - -C "$WORK/src"
meson setup "$BUILD" "$WORK/src" --prefix="$DRV_OPT" --libdir=lib --buildtype=release \
    -Ddrivers=default -Dintrospection=false -Ddoc=false -Dgtk-examples=false \
    -Dinstalled-tests=false -Dudev_rules=disabled -Dudev_hwdb=disabled >"$WORK/setup.log" 2>&1 \
    || { tail -30 "$WORK/setup.log"; exit 1; }
ninja -C "$BUILD" >"$WORK/ninja.log" 2>&1 || { tail -30 "$WORK/ninja.log"; exit 1; }
DESTDIR="$DRVROOT" meson install -C "$BUILD" --no-rebuild >/dev/null

echo "==> [$DRV] раскладка"
rm -rf "$DRVROOT$DRV_OPT/include" "$DRVROOT$DRV_OPT/lib/pkgconfig" "$DRVROOT$DRV_OPT/share"
rm -f "$DRVROOT$DRV_OPT/lib/libfprint-2.so"
strip --strip-unneeded "$DRVROOT$DRV_OPT/lib/libfprint-2.so.2.0.0"
install -D -m 0755 "$BUILD/examples/clear-storage" "$DRVROOT$DRV_OPT/libexec/clear-storage"
strip "$DRVROOT$DRV_OPT/libexec/clear-storage"
# «00-», чтобы наш каталог шёл раньше системного /usr/lib/x86_64-linux-gnu
install -d "$DRVROOT/etc/ld.so.conf.d"
echo "$DRV_OPT/lib" > "$DRVROOT/etc/ld.so.conf.d/00-$DRV.conf"
install -D -m 0644 "$SRC/COPYING" "$DRVROOT/usr/share/doc/$DRV/copyright"

mkdir -p "$WORK/shlib/debian"
printf 'Source: %s\n\nPackage: %s\nArchitecture: amd64\n' "$DRV" "$DRV" > "$WORK/shlib/debian/control"
SHLIBS=$(cd "$WORK/shlib" && dpkg-shlibdeps -O -l"$DRVROOT$DRV_OPT/lib" "$DRVROOT$DRV_OPT/lib/libfprint-2.so.2.0.0" \
    2>/dev/null | sed -n 's/^shlibs:Depends=//p')

install -d "$DRVROOT/DEBIAN"
cat > "$DRVROOT/DEBIAN/control" <<EOF
Package: $DRV
Version: $DRV_VERSION
Architecture: amd64
Maintainer: $MAINTAINER
Installed-Size: $(du -sk --exclude=DEBIAN "$DRVROOT" | cut -f1)
Depends: $SHLIBS
Recommends: fprintd (>= 1.94), libpam-fprintd, $APP
Section: libs
Priority: optional
Homepage: https://github.com/obra/libfprint-egismoc-sdcp
Description: libfprint with SDCP support for Egis Match-on-Chip readers
 A build of libfprint from the egismoc SDCP fork with support for the
 Egis ETU906 reader (USB 1c7a:05aa), installed under $DRV_OPT and
 registered with the dynamic linker ahead of the Debian libfprint, so
 fprintd uses it automatically. Includes all default libfprint drivers.
EOF
echo "/etc/ld.so.conf.d/00-$DRV.conf" > "$DRVROOT/DEBIAN/conffiles"

cat > "$DRVROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
    # Остатки ручной установки в /usr/local до появления пакета
    rm -f /usr/local/lib/x86_64-linux-gnu/libfprint-2.so /usr/local/lib/x86_64-linux-gnu/libfprint-2.so.2 \
          /usr/local/lib/x86_64-linux-gnu/libfprint-2.so.2.0.0 /usr/local/lib/x86_64-linux-gnu/pkgconfig/libfprint-2.pc \
          /usr/local/share/metainfo/org.freedesktop.libfprint.metainfo.xml
    rm -rf /usr/local/include/libfprint-2
    ldconfig
    # SDCP-сессия, закэшированная старой библиотекой, недействительна
    rm -rf /tmp/.libfprint/egismoc /var/lib/fprint/egismoc
    if [ -d /run/systemd/system ]; then
        systemctl try-restart fprintd.service >/dev/null 2>&1 || true
    fi
fi
exit 0
EOF
cat > "$DRVROOT/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
case "$1" in
    remove|purge)
        ldconfig
        if [ -d /run/systemd/system ]; then
            systemctl try-restart fprintd.service >/dev/null 2>&1 || true
        fi
        ;;
esac
exit 0
EOF
chmod 0755 "$DRVROOT/DEBIAN/postinst" "$DRVROOT/DEBIAN/postrm"
DRV_DEB="$OUT_DIR/${DRV}_${DRV_VERSION}_amd64.deb"
dpkg-deb --root-owner-group -Zxz --build "$DRVROOT" "$DRV_DEB" >/dev/null

# ================================================================ приложение
echo "==> [$APP] раскладка"
APPROOT="$WORK/app"
install -D -m 0644 "$GUI/fprint_manager.py" "$APPROOT$APP_OPT/fprint_manager.py"
install -D -m 0755 "$GUI/fprint-manager-helper" "$APPROOT/usr/libexec/fprint-manager-helper"
install -D -m 0644 "$GUI/io.github.fprintmanager.policy" "$APPROOT/usr/share/polkit-1/actions/io.github.fprintmanager.policy"
install -d "$APPROOT/usr/bin"
cat > "$APPROOT/usr/bin/$APP" <<EOF
#!/bin/sh
exec python3 $APP_OPT/fprint_manager.py "\$@"
EOF
chmod 0755 "$APPROOT/usr/bin/$APP"
install -d "$APPROOT/usr/share/applications"
cat > "$APPROOT/usr/share/applications/io.github.fprintmanager.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Fingerprints
Name[ru]=Отпечатки пальцев
Comment=Manage fingerprints and the fingerprint reader driver
Comment[ru]=Запись и удаление отпечатков, вход по пальцу, драйвер сканера
Exec=$APP
Icon=auth-fingerprint-symbolic
Terminal=false
Categories=Settings;HardwareSettings;Security;
Keywords=fingerprint;fprintd;отпечаток;палец;сканер;
StartupNotify=true
EOF
install -d "$APPROOT/usr/share/doc/$APP"
printf 'Fingerprints (fprint-manager)\nCopyright: %s\nLicense: LGPL-2.1+\n' "$MAINTAINER" > "$APPROOT/usr/share/doc/$APP/copyright"

install -d "$APPROOT/DEBIAN"
cat > "$APPROOT/DEBIAN/control" <<EOF
Package: $APP
Version: $APP_VERSION
Architecture: all
Maintainer: $MAINTAINER
Installed-Size: $(du -sk --exclude=DEBIAN "$APPROOT" | cut -f1)
Depends: fprintd (>= 1.94), libpam-fprintd, python3, python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), pkexec, polkitd
Recommends: $DRV
Section: utils
Priority: optional
Description: Fingerprints — manage fingers and the fingerprint reader
 GTK 4 / libadwaita application on top of fprintd: enroll, delete and
 test fingers, turn fingerprint login and sudo (PAM) on or off, view the
 reader and the libfprint in use, restart fprintd, read its journal and
 toggle debug logging. With $DRV it can also wipe the
 reader's on-chip storage.
EOF
cat > "$APPROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
    # Остатки ручной установки до появления пакета
    rm -f /usr/local/libexec/fprint-manager-helper /usr/local/bin/fprint-manager
    for f in /home/*/.local/share/applications/io.github.fprintmanager.desktop; do
        if [ -f "$f" ] && grep -q 'Projects/fprint' "$f"; then rm -f "$f"; fi
    done
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
fi
exit 0
EOF
cat > "$APPROOT/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = purge ]; then
    rm -f /etc/systemd/system/fprintd.service.d/fprint-manager-debug.conf
    rmdir /etc/systemd/system/fprintd.service.d 2>/dev/null || true
    if [ -d /run/systemd/system ]; then
        systemctl daemon-reload >/dev/null 2>&1 || true
        systemctl try-restart fprintd.service >/dev/null 2>&1 || true
    fi
fi
exit 0
EOF
chmod 0755 "$APPROOT/DEBIAN/postinst" "$APPROOT/DEBIAN/postrm"
APP_DEB="$OUT_DIR/${APP}_${APP_VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$APPROOT" "$APP_DEB" >/dev/null

echo
echo "Готово:"
echo "  $DRV_DEB ($(du -h "$DRV_DEB" | cut -f1))"
echo "  $APP_DEB ($(du -h "$APP_DEB" | cut -f1))"
