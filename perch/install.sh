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
installed=false
app_changed=false
cli_changed=false
launcher_changed=false
target="$install_dir/Perch.app"
backup=""
cleanup() {
  if [ "$installed" = false ]; then
    if [ "$launcher_changed" = true ]; then rm -f "$bin_dir/perch"; [ ! -e "$stage/previous-perch" ] && [ ! -L "$stage/previous-perch" ] || mv "$stage/previous-perch" "$bin_dir/perch"; fi
    if [ "$cli_changed" = true ]; then rm -f "$bin_dir/perch-cli"; [ ! -e "$stage/previous-cli" ] && [ ! -L "$stage/previous-cli" ] || mv "$stage/previous-cli" "$bin_dir/perch-cli"; fi
    if [ "$app_changed" = true ]; then rm -rf "$target"; [ -z "$backup" ] || mv "$backup" "$target"; fi
  fi
  rm -rf "$stage"
}
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

# Do not replace another program that happens to use the same command name.
for name in perch perch-cli; do
  command_path="$bin_dir/$name"
  if [ -L "$command_path" ]; then
    case "$(readlink "$command_path")" in
      */Perch.app/Contents/MacOS/perch-cli) ;;
      *) echo "$command_path belongs to another installation; left unchanged." >&2; exit 1 ;;
    esac
  elif [ -e "$command_path" ] && ! head -c 8192 "$command_path" | grep -q '^# Perch managed launcher$'; then
    echo "$command_path belongs to another program; left unchanged." >&2
    exit 1
  fi
done

# Quote a literal path without evaluating shell substitutions in folder names.
shell_path=$(printf '%s' "$target/Contents/MacOS/perch-cli" | sed 's/[\\"$`]/\\&/g')
printf '#!/bin/sh\n# Perch managed launcher\nexec "%s" "$@"\n' "$shell_path" > "$stage/launcher"
chmod 755 "$stage/launcher"

if [ -e "$target" ]; then
  backup="$install_dir/.Perch.app.previous.$(basename "$stage")"
  mv "$target" "$backup"
fi
app_changed=true
mv "$app" "$target"

if [ -e "$bin_dir/perch-cli" ] || [ -L "$bin_dir/perch-cli" ]; then mv "$bin_dir/perch-cli" "$stage/previous-cli"; fi
cli_changed=true
cp "$stage/launcher" "$bin_dir/perch-cli"
if [ -e "$bin_dir/perch" ] || [ -L "$bin_dir/perch" ]; then mv "$bin_dir/perch" "$stage/previous-perch"; fi
launcher_changed=true
cp "$stage/launcher" "$bin_dir/perch"
"$bin_dir/perch-cli" --version >/dev/null
installed=true
# Keep the previous application recoverable after a successful upgrade.

echo "Installed Perch to $target"
echo "Open Perch from Applications. Command-line launchers are optional."
