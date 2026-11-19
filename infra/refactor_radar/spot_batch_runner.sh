#!/usr/bin/env bash
set -euo pipefail

S3_RUN_URI="${1:?Usage: spot_batch_runner.sh s3://bucket/prefix}"
WORK_ROOT="${RADAR_WORK_ROOT:-/opt/dlami/nvme/radar_work}"
if [ ! -d "/opt/dlami/nvme" ]; then
  WORK_ROOT="/var/tmp/radar_work"
fi

REPOS_DIR="${WORK_ROOT}/repos"
CAPSULES_DIR="${WORK_ROOT}/capsules"
CACHE_DIR="${WORK_ROOT}/hf_cache"
KIT_DIR="${WORK_ROOT}/kit"

mkdir -p "${REPOS_DIR}" "${CAPSULES_DIR}" "${CACHE_DIR}" "${KIT_DIR}"

cleanup_and_terminate() {
  local rc=$?
  echo "[spot-batch] Syncing capsules & logs to ${S3_RUN_URI}/capsules/ (exit=${rc})"
  aws s3 sync "${CAPSULES_DIR}" "${S3_RUN_URI}/capsules/" --only-show-errors || true
  echo "[spot-batch] Initiating self-shutdown (InstanceInitiatedShutdownBehavior=terminate)"
  sudo shutdown -h now || /sbin/shutdown -h now || true
}
trap cleanup_and_terminate EXIT

# 45-minute hard dead-man timer
sudo systemd-run --on-active=45m /sbin/shutdown -h now || true

aws s3 sync "${S3_RUN_URI}/input/kit/" "${KIT_DIR}/" --only-show-errors
aws s3 sync "${S3_RUN_URI}/input/repos/" "${WORK_ROOT}/tarballs/" --only-show-errors

for tb in "${WORK_ROOT}/tarballs/"*.tar.gz; do
  [ -f "${tb}" ] || continue
  svc_name="$(basename "${tb}" .tar.gz)"
  mkdir -p "${REPOS_DIR}/${svc_name}"
  tar -xzf "${tb}" -C "${REPOS_DIR}/${svc_name}"
done

export RADAR_CACHE_DIR="${CACHE_DIR}"
export RADAR_REPOS_DIR="${REPOS_DIR}"
export RADAR_CAPSULES_DIR="${CAPSULES_DIR}"

CC="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -n1 | tr -d ' ' || echo '8.6')"
VRAM_MIB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -n1 | tr -d ' ' || echo '24000')"
echo "[spot-batch] Detected GPU compute_cap=${CC}, vram=${VRAM_MIB}MiB"
if [ "${CC}" = "7.5" ]; then
  export TEI_IMAGE="ghcr.io/huggingface/text-embeddings-inference:turing-1.5"
elif [ "${CC}" = "8.9" ]; then
  export TEI_IMAGE="ghcr.io/huggingface/text-embeddings-inference:89-1.5"
else
  export TEI_IMAGE="ghcr.io/huggingface/text-embeddings-inference:86-1.5"
fi
if [ "${VRAM_MIB:-0}" -lt 20000 ]; then
  export LLM_MODEL="Qwen/Qwen2.5-Coder-7B-Instruct-AWQ"
  export VLLM_MAX_LEN="8192"
  export VLLM_MEM_UTIL="0.68"
else
  export LLM_MODEL="Qwen/Qwen2.5-Coder-14B-Instruct-AWQ"
  export VLLM_MAX_LEN="12288"
  export VLLM_MEM_UTIL="0.70"
fi

cd "${KIT_DIR}/infra/refactor_radar"
python3 -m pip install -q huggingface_hub || true
python3 -c "from huggingface_hub import snapshot_download; snapshot_download('jinaai/jina-embeddings-v2-base-code', local_dir='${CACHE_DIR}/jina-embeddings-v2-base-code')"
export TEI_MODEL_ID="/data/jina-embeddings-v2-base-code"
docker compose up -d tei-embed vllm-coder
docker compose build analyzer

echo "[spot-batch] Waiting for TEI and vLLM readiness..."
for i in $(seq 1 120); do
  if curl -sf http://127.0.0.1:8081/health >/dev/null && curl -sf http://127.0.0.1:8000/v1/models >/dev/null; then
    echo "[spot-batch] GPU inference engines ready after ${i}x5s"
    break
  fi
  sleep 5
done

for svc_dir in "${REPOS_DIR}"/*; do
  [ -d "${svc_dir}" ] || continue
  svc="$(basename "${svc_dir}")"
  echo "[spot-batch] Analyzing ${svc}..."
  docker compose run --rm analyzer analyze \
    --repo "/repos/${svc}" \
    --service-name "${svc}" \
    --capsule-out "/capsules/${svc}" \
    --embed-backend tei
done

echo "[spot-batch] Running Phase 2 Federation..."
docker compose run --rm analyzer federate \
  --capsules-root /capsules \
  --out-dir /capsules/_federated

echo "[spot-batch] Batch completed cleanly."

