#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${ROOT_DIR}/.runtime"
PYTHON_ENV="${RUNTIME_DIR}/python"
HERMES_ENV="${RUNTIME_DIR}/hermes"
UV_ENV="${RUNTIME_DIR}/uv"
BIN_DIR="${RUNTIME_DIR}/bin"
CHECKSUMS="${ROOT_DIR}/installers/tectonic-0.16.9.sha256"
TECTONIC_VERSION="0.16.9"
NO_START="false"

if [[ "${1:-}" == "--no-start" ]]; then
  NO_START="true"
elif [[ $# -gt 0 ]]; then
  echo "Usage: ./install.sh [--no-start]" >&2
  exit 2
fi

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="${PYTHON}"
elif command -v python3.12 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3.12)"
elif command -v python3 >/dev/null 2>&1 \
  && python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
  PYTHON_BIN="$(command -v python3)"
elif command -v uv >/dev/null 2>&1; then
  PYTHON_BIN="$(uv python find 3.12 2>/dev/null || true)"
else
  PYTHON_BIN=""
fi
if [[ -z "${PYTHON_BIN}" ]] \
  || ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
  echo "Python 3.12 or newer is required." >&2
  exit 1
fi
command -v node >/dev/null 2>&1 || {
  echo "Node.js 20.9 or newer is required." >&2
  exit 1
}
node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit(major > 20 || (major === 20 && minor >= 9) ? 0 : 1)' || {
  echo "Node.js 20.9 or newer is required." >&2
  exit 1
}
command -v npm >/dev/null 2>&1 || {
  echo "npm is required." >&2
  exit 1
}
command -v curl >/dev/null 2>&1 || {
  echo "curl is required to download Tectonic." >&2
  exit 1
}

mkdir -p "${RUNTIME_DIR}" "${BIN_DIR}"
"${PYTHON_BIN}" -m venv "${PYTHON_ENV}"
"${PYTHON_BIN}" -m venv "${UV_ENV}"
"${UV_ENV}/bin/python" -m pip install --disable-pip-version-check "uv==0.11.6"
UV_PROJECT_ENVIRONMENT="${PYTHON_ENV}" "${UV_ENV}/bin/uv" sync \
  --project "${ROOT_DIR}" --frozen --no-dev --extra companion
"${PYTHON_BIN}" -m venv "${HERMES_ENV}"
"${HERMES_ENV}/bin/python" -m pip install --disable-pip-version-check -r "${ROOT_DIR}/agent-profile/requirements-hermes.txt"

pushd "${ROOT_DIR}/frontend" >/dev/null
npm ci
CAREERPILOT_STATIC_EXPORT=true NEXT_PUBLIC_API_BASE_URL= npm run build
popd >/dev/null

if [[ "$(uname -s)" == "Linux" ]]; then
  "${PYTHON_ENV}/bin/python" -m playwright install --with-deps chromium
else
  "${PYTHON_ENV}/bin/python" -m playwright install chromium
fi

OS_NAME="$(uname -s)"
ARCH_NAME="$(uname -m)"
case "${OS_NAME}:${ARCH_NAME}" in
  Darwin:arm64|Darwin:aarch64)
    TECTONIC_ASSET="tectonic-${TECTONIC_VERSION}-aarch64-apple-darwin.tar.gz"
    ;;
  Darwin:x86_64|Darwin:amd64)
    TECTONIC_ASSET="tectonic-${TECTONIC_VERSION}-x86_64-apple-darwin.tar.gz"
    ;;
  Linux:arm64|Linux:aarch64)
    TECTONIC_ASSET="tectonic-${TECTONIC_VERSION}-aarch64-unknown-linux-musl.tar.gz"
    ;;
  Linux:x86_64|Linux:amd64)
    TECTONIC_ASSET="tectonic-${TECTONIC_VERSION}-x86_64-unknown-linux-gnu.tar.gz"
    ;;
  *)
    echo "No native Tectonic build is available for ${OS_NAME} ${ARCH_NAME}. Use Docker." >&2
    exit 1
    ;;
esac

EXPECTED_SHA256="$(awk -v asset="${TECTONIC_ASSET}" '$2 == asset {print $1}' "${CHECKSUMS}")"
if [[ -z "${EXPECTED_SHA256}" ]]; then
  echo "Tectonic checksum manifest is incomplete." >&2
  exit 1
fi
ARCHIVE_PATH="$(mktemp "${TMPDIR:-/tmp}/career-companion-tectonic.XXXXXX")"
trap 'rm -f "${ARCHIVE_PATH}"' EXIT
curl --proto '=https' --tlsv1.2 --fail --location --output "${ARCHIVE_PATH}" \
  "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/${TECTONIC_ASSET}"
if command -v shasum >/dev/null 2>&1; then
  ACTUAL_SHA256="$(shasum -a 256 "${ARCHIVE_PATH}" | awk '{print $1}')"
else
  ACTUAL_SHA256="$(sha256sum "${ARCHIVE_PATH}" | awk '{print $1}')"
fi
if [[ "${ACTUAL_SHA256}" != "${EXPECTED_SHA256}" ]]; then
  echo "Tectonic checksum verification failed." >&2
  exit 1
fi
tar -xzf "${ARCHIVE_PATH}" -C "${BIN_DIR}"
chmod 0755 "${BIN_DIR}/tectonic"

PLAYWRIGHT_SHA256="$("${PYTHON_ENV}/bin/python" -c 'from career_companion.playwright_integrity import chromium_sha256; print(chromium_sha256()[1])')"
"${PYTHON_ENV}/bin/career-companion" setup \
  --hermes-executable "${HERMES_ENV}/bin/hermes" \
  --tectonic-executable "${BIN_DIR}/tectonic" \
  --playwright-checksum "${PLAYWRIGHT_SHA256}"
"${PYTHON_ENV}/bin/career-companion" doctor

echo "Career Companion is installed."
echo "Start it later with: ${PYTHON_ENV}/bin/career-companion start"
if [[ "${NO_START}" == "false" ]]; then
  exec "${PYTHON_ENV}/bin/career-companion" start
fi
