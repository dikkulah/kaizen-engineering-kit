#!/usr/bin/env bash
# Kaizen Cloud-Brain — Idempotent Ubuntu 24.04 LTS (ARM64 / x86_64) Bootstrap
#
# Installs rootless Podman + podman-compose, tunes kernel VM sysctls
# (vm.swappiness=10 & transparent/static hugepages for Sparse-MoE KV cache),
# enables systemd user linger (loginctl enable-linger), installs the optional
# idle-stop timer, and starts the 7-port loopback Cloud-Brain stack.

set -Eeuo pipefail
IFS=$'\n\t'

KAIZEN_USER="${KAIZEN_USER:-kaizen}"
KAIZEN_INSTALL_DIR="${KAIZEN_INSTALL_DIR:-/opt/kaizen-cloud-brain}"
KAIZEN_WORKSPACE="${KAIZEN_WORKSPACE:-/work-space}"
KAIZEN_MODELS_DIR="${KAIZEN_MODELS_DIR:-/var/lib/kaizen/models}"
KAIZEN_STATE_DIR="${KAIZEN_STATE_DIR:-/var/lib/kaizen/state}"
KAIZEN_BIND_ADDR="${KAIZEN_BIND_ADDR:-127.0.0.1}"
KAIZEN_S3_MODEL_BUCKET="${KAIZEN_S3_MODEL_BUCKET:-}"
KAIZEN_GCS_MODEL_BUCKET="${KAIZEN_GCS_MODEL_BUCKET:-}"
KAIZEN_ENABLE_IDLE_GUARD="${KAIZEN_ENABLE_IDLE_GUARD:-true}"
KAIZEN_IDLE_MINUTES="${KAIZEN_IDLE_MINUTES:-45}"
KAIZEN_NR_HUGEPAGES="${KAIZEN_NR_HUGEPAGES:-0}"
KAIZEN_REPO_URL="${KAIZEN_REPO_URL:-https://github.com/kaizen-kit/kaizen-engineering-kit.git}"
KAIZEN_REPO_REF="${KAIZEN_REPO_REF:-main}"
KAIZEN_DRY_RUN="${KAIZEN_DRY_RUN:-false}"

log() {
  printf '[kaizen-cloud-brain] %s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: bootstrap-cloud-brain.sh must run as root (or via cloud-init)." >&2
    exit 1
  fi
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  log "Installing rootless Podman, podman-compose, and runtime prerequisites..."
  apt-get update -qq
  apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    dbus-user-session \
    fuse-overlayfs \
    git \
    jq \
    passt \
    podman \
    podman-compose \
    python3 \
    python3-yaml \
    slirp4netns \
    uidmap \
    wget \
    zstd
}

tune_kernel_memory() {
  log "Configuring kernel memory parameters (vm.swappiness=10 & hugepages)..."
  cat >/etc/sysctl.d/99-kaizen-cloud-brain.conf <<EOF
# Kaizen Cloud-Brain 128 GiB Sparse-MoE & In-RAM LSP tuning
vm.swappiness = 10
vm.vfs_cache_pressure = 50
vm.max_map_count = 262144
vm.overcommit_memory = 1
vm.nr_hugepages = ${KAIZEN_NR_HUGEPAGES}
EOF
  sysctl --system >/dev/null

  # Configure Transparent Hugepages to 'madvise' so ik_llama.cpp / LanceDB get
  # 2 MiB hugepage TLB efficiency without system-wide compaction stalls.
  cat >/etc/tmpfiles.d/kaizen-hugepages.conf <<'EOF'
w /sys/kernel/mm/transparent_hugepage/enabled - - - - madvise
w /sys/kernel/mm/transparent_hugepage/defrag  - - - - defer+madvise
EOF
  systemd-tmpfiles --create /etc/tmpfiles.d/kaizen-hugepages.conf || true
}

ensure_rootless_user() {
  if ! id -u "${KAIZEN_USER}" >/dev/null 2>&1; then
    log "Creating dedicated rootless service account '${KAIZEN_USER}'..."
    useradd --create-home --shell /bin/bash --user-group "${KAIZEN_USER}"
  fi

  if ! grep -q "^${KAIZEN_USER}:" /etc/subuid 2>/dev/null; then
    usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "${KAIZEN_USER}"
  fi

  log "Enabling systemd user linger for '${KAIZEN_USER}'..."
  loginctl enable-linger "${KAIZEN_USER}"
}

prepare_directories() {
  log "Preparing Cloud-Brain workspace, model cache, and index directories..."
  install -d -m 0755 -o "${KAIZEN_USER}" -g "${KAIZEN_USER}" \
    "${KAIZEN_INSTALL_DIR}" \
    "${KAIZEN_WORKSPACE}" \
    "${KAIZEN_MODELS_DIR}" \
    "${KAIZEN_MODELS_DIR}/tei" \
    "${KAIZEN_STATE_DIR}" \
    "${KAIZEN_STATE_DIR}/guardrail" \
    "${KAIZEN_STATE_DIR}/gateway" \
    "/var/lib/kaizen/zoekt-index" \
    "/var/lib/kaizen/coderag" \
    "/var/lib/kaizen/serena-cache"
}

stage_compose_files() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  if [[ -f "${script_dir}/docker-compose.yml" ]]; then
    install -m 0644 -o "${KAIZEN_USER}" -g "${KAIZEN_USER}" \
      "${script_dir}/docker-compose.yml" "${KAIZEN_INSTALL_DIR}/docker-compose.yml"
  elif [[ ! -f "${KAIZEN_INSTALL_DIR}/docker-compose.yml" ]]; then
    log "Fetching kaizen-engineering-kit (${KAIZEN_REPO_REF}) to stage docker-compose.yml..."
    local tmp_clone
    tmp_clone="$(mktemp -d)"
    git clone --depth 1 --branch "${KAIZEN_REPO_REF}" "${KAIZEN_REPO_URL}" "${tmp_clone}"
    install -m 0644 -o "${KAIZEN_USER}" -g "${KAIZEN_USER}" \
      "${tmp_clone}/infra/cloud_brain/docker-compose.yml" "${KAIZEN_INSTALL_DIR}/docker-compose.yml"
    rm -rf "${tmp_clone}"
  fi

  local cpu_threads
  cpu_threads="$(nproc --ignore=4 2>/dev/null || echo 28)"
  if [[ "${cpu_threads}" -lt 4 ]]; then
    cpu_threads=4
  fi

  if [[ ! -f "${KAIZEN_INSTALL_DIR}/.env" ]]; then
    cat >"${KAIZEN_INSTALL_DIR}/.env" <<EOF
KAIZEN_BIND_ADDR=${KAIZEN_BIND_ADDR}
KAIZEN_WORKSPACE=${KAIZEN_WORKSPACE}
KAIZEN_MODELS_DIR=${KAIZEN_MODELS_DIR}
KAIZEN_STATE_DIR=${KAIZEN_STATE_DIR}
KAIZEN_ZOEKT_INDEX_DIR=/var/lib/kaizen/zoekt-index
KAIZEN_CODERAG_DIR=/var/lib/kaizen/coderag
KAIZEN_SERENA_CACHE_DIR=/var/lib/kaizen/serena-cache
KAIZEN_MOE_THREADS=${cpu_threads}
EOF
    chown "${KAIZEN_USER}:${KAIZEN_USER}" "${KAIZEN_INSTALL_DIR}/.env"
    chmod 0600 "${KAIZEN_INSTALL_DIR}/.env"
  fi
}

sync_optional_models() {
  if [[ -n "${KAIZEN_S3_MODEL_BUCKET}" ]] && command -v aws >/dev/null 2>&1; then
    log "Syncing GGUF models from s3://${KAIZEN_S3_MODEL_BUCKET}/models/ ..."
    su - "${KAIZEN_USER}" -c \
      "aws s3 sync 's3://${KAIZEN_S3_MODEL_BUCKET}/models/' '${KAIZEN_MODELS_DIR}/' --only-show-errors" || true
  fi

  if [[ -n "${KAIZEN_GCS_MODEL_BUCKET}" ]] && command -v gcloud >/dev/null 2>&1; then
    log "Syncing GGUF models from gs://${KAIZEN_GCS_MODEL_BUCKET}/models/ ..."
    su - "${KAIZEN_USER}" -c \
      "gcloud storage rsync 'gs://${KAIZEN_GCS_MODEL_BUCKET}/models/' '${KAIZEN_MODELS_DIR}/' --quiet" || true
  fi
}

install_systemd_user_service() {
  local user_home user_uid unit_dir
  user_home="$(getent passwd "${KAIZEN_USER}" | cut -d: -f6)"
  user_uid="$(id -u "${KAIZEN_USER}")"
  unit_dir="${user_home}/.config/systemd/user"

  install -d -m 0755 -o "${KAIZEN_USER}" -g "${KAIZEN_USER}" \
    "${user_home}/.config" "${user_home}/.config/systemd" "${unit_dir}"

  cat >"${unit_dir}/kaizen-cloud-brain.service" <<EOF
[Unit]
Description=Kaizen Cloud-Brain Rootless Podman Stack (7 Loopback Ports)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${KAIZEN_INSTALL_DIR}
EnvironmentFile=${KAIZEN_INSTALL_DIR}/.env
ExecStart=/usr/bin/podman compose -f ${KAIZEN_INSTALL_DIR}/docker-compose.yml up -d --remove-orphans
ExecStop=/usr/bin/podman compose -f ${KAIZEN_INSTALL_DIR}/docker-compose.yml down
TimeoutStartSec=900

[Install]
WantedBy=default.target
EOF
  chown "${KAIZEN_USER}:${KAIZEN_USER}" "${unit_dir}/kaizen-cloud-brain.service"

  if [[ "${KAIZEN_DRY_RUN}" == "true" ]]; then
    log "Dry-run enabled; skipping podman pull and service activation."
    return 0
  fi

  log "Activating rootless systemd unit kaizen-cloud-brain.service for UID ${user_uid}..."
  systemctl start "user@${user_uid}.service"
  su - "${KAIZEN_USER}" -c \
    "XDG_RUNTIME_DIR=/run/user/${user_uid} systemctl --user daemon-reload && \
     XDG_RUNTIME_DIR=/run/user/${user_uid} systemctl --user enable --now kaizen-cloud-brain.service"
}

install_idle_guard() {
  if [[ "${KAIZEN_ENABLE_IDLE_GUARD}" != "true" ]]; then
    log "Idle guard disabled (KAIZEN_ENABLE_IDLE_GUARD=${KAIZEN_ENABLE_IDLE_GUARD})."
    return 0
  fi

  log "Installing idle-stop guard (${KAIZEN_IDLE_MINUTES} min inactivity threshold)..."
  cat >/usr/local/bin/kaizen-idle-guard.sh <<EOF
#!/usr/bin/env bash
set -euo pipefail
STATE_FILE="/run/kaizen-idle-counter"
IDLE_LIMIT_CHECKS=\$(( ${KAIZEN_IDLE_MINUTES} / 5 ))
if [[ "\${IDLE_LIMIT_CHECKS}" -lt 1 ]]; then
  IDLE_LIMIT_CHECKS=1
fi

# Detect active loopback tunnel sockets across the 7 Cloud-Brain ports or CPU load > 0.35
ACTIVE_CONNS=\$(ss -Htan '( sport = :4000 or sport = :6070 or sport = :8765 or sport = :8766 or sport = :8770 or sport = :18808 or sport = :24280 )' | grep -c ESTAB || true)
LOAD_INT=\$(awk '{ printf "%d", \$1 * 100 }' /proc/loadavg)

if [[ "\${ACTIVE_CONNS}" -gt 0 || "\${LOAD_INT}" -gt 35 ]]; then
  echo 0 >"\${STATE_FILE}"
  exit 0
fi

COUNT=0
if [[ -f "\${STATE_FILE}" ]]; then
  COUNT=\$(cat "\${STATE_FILE}" || echo 0)
fi
COUNT=\$(( COUNT + 1 ))
echo "\${COUNT}" >"\${STATE_FILE}"

if [[ "\${COUNT}" -ge "\${IDLE_LIMIT_CHECKS}" ]]; then
  logger -t kaizen-idle-guard "Cloud-Brain idle for ${KAIZEN_IDLE_MINUTES}m; initiating clean shutdown."
  rm -f "\${STATE_FILE}"
  systemctl poweroff
fi
EOF
  chmod 0755 /usr/local/bin/kaizen-idle-guard.sh

  cat >/etc/systemd/system/kaizen-idle-guard.service <<'EOF'
[Unit]
Description=Kaizen Cloud-Brain Idle Activity Check

[Service]
Type=oneshot
ExecStart=/usr/local/bin/kaizen-idle-guard.sh
EOF

  cat >/etc/systemd/system/kaizen-idle-guard.timer <<'EOF'
[Unit]
Description=Run Kaizen Cloud-Brain Idle Guard Every 5 Minutes

[Timer]
OnBootSec=15min
OnUnitActiveSec=5min
Unit=kaizen-idle-guard.service

[Install]
WantedBy=timers.target
EOF

  systemctl daemon-reload
  systemctl enable --now kaizen-idle-guard.timer
}

main() {
  require_root
  install_packages
  tune_kernel_memory
  ensure_rootless_user
  prepare_directories
  stage_compose_files
  sync_optional_models
  install_systemd_user_service
  install_idle_guard
  log "Bootstrap complete. Cloud-Brain 7-port stack bound to ${KAIZEN_BIND_ADDR} (:4000 :6070 :8765 :8766 :8770 :18808 :24280)."
}

main "$@"
