#!/usr/bin/env sh
# Install the Apple silicon macOS release without Python or a local checkout.
set -eu

repo_base="${PERCH_DOWNLOAD_BASE:-https://github.com/soyr-redhat/perch/releases}"
version="${PERCH_VERSION:-latest}"
install_dir="${PERCH_INSTALL_DIR:-$HOME/Applications}"
bin_dir="${PERCH_BIN_DIR:-$HOME/.local/bin}"
asset="Perch-macOS-arm64.zip"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Perch's shell installer supports macOS. Use perch/install.ps1 on Windows." >&2
  exit 1
fi
case "$(uname -m)" in
  arm64|aarch64) ;;
  *)
    echo "This release is for Apple silicon Macs. Intel macOS builds are not published yet." >&2
    exit 1
    ;;
esac

if [ "$version" = "latest" ]; then
  download_base="$repo_base/latest/download"
else
  download_base="$repo_base/download/$version"
fi

mkdir -p "$install_dir" "$bin_dir"
stage="$(mktemp -d "$install_dir/.perch-install.XXXXXX")"
cleanup() { rm -rf "$stage"; }
trap cleanup EXIT HUP INT TERM

archive="$stage/$asset"
checksums="$stage/SHA256SUMS"
curl --fail --location --silent --show-error "$download_base/$asset" --output "$archive"
curl --fail --location --silent --show-error "$download_base/SHA256SUMS" --output "$checksums"

expected="$(awk -v asset="$asset" '$2 == asset || $2 == "*" asset { print $1; exit }' "$checksums")"
actual="$(shasum -a 256 "$archive" | awk '{ print $1 }')"
if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
  echo "Release checksum verification failed; Perch was not installed." >&2
  exit 1
fi

unzip -q "$archive" -d "$stage/unpacked"
app="$stage/unpacked/Perch.app"
if [ ! -x "$app/Contents/MacOS/perch-cli" ]; then
  echo "Release archive is missing Perch.app." >&2
  exit 1
fi

target="$install_dir/Perch.app"
backup=""
if [ -e "$target" ]; then
  backup="$install_dir/.Perch.app.previous.$$"
  mv "$target" "$backup"
fi
if ! mv "$app" "$target"; then
  [ -z "$backup" ] || mv "$backup" "$target"
  echo "Could not install Perch." >&2
  exit 1
fi
[ -z "$backup" ] || rm -rf "$backup"

ln -sfn "$target/Contents/MacOS/perch-cli" "$bin_dir/perch"
ln -sfn "$target/Contents/MacOS/perch-cli" "$bin_dir/perch-cli"

echo "Installed Perch to $target"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) echo "Add $bin_dir to PATH to use perch from a terminal." ;;
esac
echo "Open it with: open $target"
