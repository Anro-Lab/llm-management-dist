#!/usr/bin/env bash
# Install Anro LLM Linux host system-info service as a systemd unit (port 8088).
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root: sudo $0" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/anro-llm-host-service}"
SERVICE_NAME="anro-llm-host-system.service"
UNIT_SRC="${SCRIPT_DIR}/systemd/${SERVICE_NAME}"
PYTHON_BIN="$(command -v python3 || true)"

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "ERROR: python3 not found" >&2
  exit 1
fi

mkdir -p "${INSTALL_DIR}"
cp -f "${SCRIPT_DIR}/linux_host_service.py" "${INSTALL_DIR}/linux_host_service.py"
chmod 755 "${INSTALL_DIR}/linux_host_service.py"

"${PYTHON_BIN}" -m pip install --upgrade pip >/dev/null
"${PYTHON_BIN}" -m pip install fastapi uvicorn psutil >/dev/null

if [[ ! -f "${UNIT_SRC}" ]]; then
  echo "ERROR: missing unit file ${UNIT_SRC}" >&2
  exit 1
fi

# Rewrite WorkingDirectory / ExecStart for install path
sed \
  -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
  -e "s|__PYTHON_BIN__|${PYTHON_BIN}|g" \
  "${UNIT_SRC}" > "/etc/systemd/system/${SERVICE_NAME}"

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

sleep 1
if curl -sf --max-time 3 "http://127.0.0.1:8088/health" >/dev/null; then
  echo "Installed and healthy: http://127.0.0.1:8088/health"
  curl -s "http://127.0.0.1:8088/health"
  echo
else
  echo "WARNING: service installed but /health not responding yet. Check: journalctl -u ${SERVICE_NAME} -f" >&2
  systemctl --no-pager --full status "${SERVICE_NAME}" || true
  exit 1
fi
