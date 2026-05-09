#!/usr/bin/env bash
# Build a Flatpak bundle of Shoebox for one or more architectures.
#
# Usage: build-flatpak.sh [arch...]
#   No args  → builds for x86_64 and aarch64.
#   With args → builds only the listed architectures.
#
# Cross-architecture builds require qemu-user-static with binfmt_misc registered
# (Fedora: `sudo dnf install qemu-user-static`,
#  Debian/Ubuntu: `sudo apt install qemu-user-static binfmt-support`).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MANIFEST="$SCRIPT_DIR/land.rob.shoebox.json"
APP_ID=land.rob.shoebox
RUNTIME_VERSION='50'
DIST_DIR="$REPO_ROOT/dist"
DEFAULT_ARCHES=(x86_64 aarch64)

usage() {
    sed -n '2,/^set /p' "$0" | sed 's/^# \{0,1\}//;$d'
}

case "${1:-}" in
    -h|--help) usage; exit 0;;
esac

# --- preflight ---------------------------------------------------------------

for tool in flatpak flatpak-builder awk; do
    command -v "$tool" >/dev/null \
        || { echo "error: $tool not installed" >&2; exit 1; }
done

[[ -f "$MANIFEST" ]] \
    || { echo "error: manifest not found: $MANIFEST" >&2; exit 1; }

if [[ $# -gt 0 ]]; then
    ARCHES=("$@")
else
    ARCHES=("${DEFAULT_ARCHES[@]}")
fi

HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
    arm64) HOST_ARCH=aarch64;;
    amd64) HOST_ARCH=x86_64;;
esac

VERSION="$(awk -F"'" '/^[[:space:]]*version:/{print $2; exit}' "$REPO_ROOT/meson.build")"
VERSION="${VERSION:-0.0.0}"

# Ensure flathub is registered (user-level only — never touches system remotes).
if ! flatpak --user remotes --columns=name | grep -qx flathub; then
    echo "==> Adding flathub remote (user)"
    flatpak --user remote-add --if-not-exists flathub \
        https://flathub.org/repo/flathub.flatpakrepo
fi

mkdir -p "$DIST_DIR"

# --- per-arch build ----------------------------------------------------------

build_one() {
    local arch="$1"
    local builddir="$SCRIPT_DIR/.build-$arch"
    local repo="$SCRIPT_DIR/.repo-$arch"
    local statedir="$SCRIPT_DIR/.state-$arch"
    local bundle="$DIST_DIR/${APP_ID}-${VERSION}-${arch}.flatpak"

    echo
    echo "==> $APP_ID  $VERSION  ($arch)"

    if [[ "$arch" != "$HOST_ARCH" ]]; then
        local marker="/proc/sys/fs/binfmt_misc/qemu-${arch}"
        if [[ ! -e "$marker" ]]; then
            echo "skip: cross-build for $arch needs qemu-user-static binfmt;" \
                 "missing $marker" >&2
            return 2
        fi
    fi

    flatpak --user install -y --noninteractive --or-update --arch="$arch" flathub \
        "org.gnome.Platform//${RUNTIME_VERSION}" \
        "org.gnome.Sdk//${RUNTIME_VERSION}"

    rm -rf "$builddir" "$repo"
    flatpak-builder \
        --user --force-clean --disable-rofiles-fuse \
        --arch="$arch" \
        --state-dir="$statedir" \
        --install-deps-from=flathub \
        --repo="$repo" \
        "$builddir" "$MANIFEST"

    flatpak build-bundle --arch="$arch" "$repo" "$bundle" "$APP_ID"
    echo "==> wrote $bundle"
}

# --- driver ------------------------------------------------------------------

failed=()
skipped=()
for arch in "${ARCHES[@]}"; do
    rc=0
    build_one "$arch" || rc=$?
    case "$rc" in
        0) ;;
        2) skipped+=("$arch");;
        *) failed+=("$arch");;
    esac
done

echo
echo "Bundles in $DIST_DIR:"
ls -1 "$DIST_DIR" 2>/dev/null || true
[[ ${#skipped[@]} -gt 0 ]] && echo "skipped: ${skipped[*]}" >&2
[[ ${#failed[@]}  -gt 0 ]] && { echo "failed: ${failed[*]}"  >&2; exit 1; }
exit 0
