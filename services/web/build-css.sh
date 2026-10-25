#!/usr/bin/env bash
# Builds static/app.css from styles/app.css with a pinned Tailwind CLI, so pages
# need no CSS compiler or CDN at runtime. Run it after changing classes in
# templates/ or static/*.js (the pre-commit hook does this too).
#
#   services/web/build-css.sh           # build once
#   services/web/build-css.sh --watch   # rebuild on every change
#
# The CLI is downloaded once into runtime/tools/ and checked against the
# SHA-256 that Tailwind published for the release.
set -euo pipefail

VERSION=4.3.1
case "$(uname -s)-$(uname -m)" in
Linux-x86_64) asset=tailwindcss-linux-x64 sha=2526d063ba03b71f9a3ea7d5cee14f0aec147f117f222d5adc97b1d736d45999 ;;
Linux-aarch64 | Linux-arm64) asset=tailwindcss-linux-arm64 sha=3d662377a86d71c43b549dc06b90db4586b4acd412bf827a3268e951661e5adf ;;
Darwin-arm64) asset=tailwindcss-macos-arm64 sha=a27c43626185953ee19bdace1939c7601e55da654e0b2fc4461e3e29957aa739 ;;
Darwin-x86_64) asset=tailwindcss-macos-x64 sha=e9e830ceb3e70b7e0775a3dd79eee8ec82c6b31270f08f2fa2857d0077045ac3 ;;
*)
	echo "No pinned Tailwind CLI for $(uname -s) $(uname -m); build inside the dev container." >&2
	exit 1
	;;
esac

web="$(cd "$(dirname "$0")" && pwd)"
bin="$web/../../runtime/tools/$asset-$VERSION"

if [ ! -x "$bin" ]; then
	mkdir -p "$(dirname "$bin")"
	tmp="$bin.download"
	echo "Downloading Tailwind CLI $VERSION ($asset)…" >&2
	curl -fsSL "https://github.com/tailwindlabs/tailwindcss/releases/download/v$VERSION/$asset" -o "$tmp"
	if command -v sha256sum >/dev/null; then
		actual="$(sha256sum "$tmp" | cut -d' ' -f1)"
	else
		actual="$(shasum -a 256 "$tmp" | cut -d' ' -f1)"
	fi
	if [ "$actual" != "$sha" ]; then
		rm -f "$tmp"
		echo "Checksum mismatch for $asset: expected $sha, got $actual" >&2
		exit 1
	fi
	chmod +x "$tmp"
	mv "$tmp" "$bin"
fi

exec "$bin" -i "$web/styles/app.css" -o "$web/static/app.css" --minify "$@"
