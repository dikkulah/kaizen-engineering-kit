"""Ephemeral AWS EC2 GPU Spot Batch Orchestrator for Refactor Radar (`kaizen-u2`).

Backwards-compatible shim delegating Spot placement scoring, `runai_streamer`,
IMDSv2 2-minute interruption checkpointing, 15-minute dead-man idle-stop, and
dry-run orchestration to `kaizen_kit.gpu_burst_orchestrator`.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tarfile
import time
from typing import Any
import uuid

import boto3
from botocore.exceptions import ClientError

from kaizen_kit import gpu_burst_orchestrator as _orchestrator

DEFAULT_PROFILE = _orchestrator.DEFAULT_PROFILE
DEFAULT_REGION = _orchestrator.DEFAULT_REGION
DEFAULT_BUCKET = _orchestrator.DEFAULT_BUCKET
DEFAULT_AMI = _orchestrator.DEFAULT_AMI
DEFAULT_SG = _orchestrator.DEFAULT_SG
DEFAULT_IAM_PROFILE = _orchestrator.DEFAULT_IAM_PROFILE
DEFAULT_PRIVATE_SUBNETS = _orchestrator.DEFAULT_PRIVATE_SUBNETS
DEFAULT_INSTANCE_TYPES = _orchestrator.DEFAULT_INSTANCE_TYPES
DEFAULT_EU_WEST_1_SPOT_TYPES: tuple[str, ...] = ("g4dn.12xlarge", "g4dn.2xlarge", "g4dn.xlarge")

_EXCLUDE_DIR_NAMES = {
    "target",
    "build",
    "node_modules",
    ".venv",
    ".venv-radar",
    "__pycache__",
    ".pytest_cache",
    ".idea",
    "dist",
}


def _should_exclude_tar(rel_parts: tuple[str, ...]) -> bool:
    for part in rel_parts:
        if part in _EXCLUDE_DIR_NAMES or part.startswith(".venv"):
            return True
    return False


def _tar_dir_to_bytes(root: Path, include_git: bool = True) -> bytes:
    import io

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=5) as tf:
        for dirpath, dirnames, filenames in os.walk(root):
            cur = Path(dirpath)
            rel = cur.relative_to(root)
            dirnames[:] = [
                d
                for d in dirnames
                if d not in _EXCLUDE_DIR_NAMES
                and not d.startswith(".venv")
                and (include_git or d != ".git")
            ]
            if _should_exclude_tar(rel.parts):
                continue
            for fn in filenames:
                fp = cur / fn
                if fp.suffix.lower() in {".pyc", ".pyo", ".exe", ".dll", ".pdb", ".so", ".dylib"}:
                    continue
                arcname = (rel / fn).as_posix() if str(rel) != "." else fn
                try:
                    if fp.is_file() and fp.stat().st_size <= 50 * 1024 * 1024:
                        tf.add(fp, arcname=arcname, recursive=False)
                except OSError:
                    continue
    return buf.getvalue()


def stage_inputs_to_s3(
    s3_client: Any,
    bucket: str,
    prefix: str,
    repos: list[tuple[str, Path]],
    kit_root: Path,
) -> None:
    kit_tar = _tar_dir_to_bytes(kit_root, include_git=False)
    s3_client.put_object(Bucket=bucket, Key=f"{prefix}/input/kit.tar.gz", Body=kit_tar)
    for svc_name, repo_path in repos:
        repo_tar = _tar_dir_to_bytes(repo_path, include_git=True)
        s3_client.put_object(
            Bucket=bucket,
            Key=f"{prefix}/input/repos/{svc_name}.tar.gz",
            Body=repo_tar,
        )


def _build_userdata(auto_shutdown_minutes: int, run_id: str = "radar-spot") -> str:
    """Delegate IMDSv2 + S3 checkpoint + idle-stop userdata to unified orchestrator."""
    return _orchestrator.build_spot_userdata(
        run_id=run_id,
        bucket=DEFAULT_BUCKET,
        checkpoint_prefix=_orchestrator.DEFAULT_CHECKPOINT_PREFIX,
        region=DEFAULT_REGION,
        idle_stop_minutes=auto_shutdown_minutes,
        encode_base64=True,
    )


def _decode_userdata_for_boto3(raw_userdata: str) -> str:
    """Decode base64 userdata to UTF-8 script because boto3 run_instances base64-encodes string UserData."""
    if raw_userdata.lstrip().startswith("#!"):
        return raw_userdata
    try:
        return base64.b64decode(raw_userdata.strip().encode("ascii"), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return raw_userdata


def launch_one_time_spot_instance(
    ec2_client: Any,
    ami_id: str,
    subnet_ids: tuple[str, ...],
    sg_id: str,
    iam_profile_arn: str,
    instance_types: tuple[str, ...],
    spot_max_price: str,
    auto_shutdown_minutes: int,
    run_id: str,
) -> tuple[str, str, str]:
    last_err: Exception | None = None
    userdata_script = _decode_userdata_for_boto3(_build_userdata(auto_shutdown_minutes, run_id=run_id))
    for itype in instance_types:
        for subnet_id in subnet_ids:
            try:
                resp = ec2_client.run_instances(
                    ImageId=ami_id,
                    InstanceType=itype,
                    MinCount=1,
                    MaxCount=1,
                    SubnetId=subnet_id,
                    SecurityGroupIds=[sg_id],
                    IamInstanceProfile={"Arn": iam_profile_arn},
                    InstanceInitiatedShutdownBehavior="terminate",
                    InstanceMarketOptions={
                        "MarketType": "spot",
                        "SpotOptions": {
                            "MaxPrice": spot_max_price,
                            "SpotInstanceType": "one-time",
                            "InstanceInterruptionBehavior": "terminate",
                        },
                    },
                    BlockDeviceMappings=[
                        {
                            "DeviceName": "/dev/sda1",
                            "Ebs": {
                                "VolumeSize": 100,
                                "VolumeType": "gp3",
                                "DeleteOnTermination": True,
                                "Encrypted": True,
                            },
                        }
                    ],
                    UserData=userdata_script,
                    TagSpecifications=[
                        {
                            "ResourceType": "instance",
                            "Tags": [
                                {"Key": "Name", "Value": f"refactor-radar-spot-{run_id}"},
                                {"Key": "Project", "Value": "refactor-radar"},
                                {"Key": "RunId", "Value": run_id},
                                {"Key": "AutoTerminate", "Value": "true"},
                            ],
                        }
                    ],
                )
                inst = resp["Instances"][0]
                return str(inst["InstanceId"]), itype, subnet_id
            except ClientError as exc:
                last_err = exc
                code = exc.response.get("Error", {}).get("Code", "")
                if code in (
                    "InsufficientInstanceCapacity",
                    "SpotMaxPriceTooLow",
                    "Unsupported",
                ):
                    continue
                raise
    raise RuntimeError(f"Failed to launch Spot GPU instance across {instance_types} / {subnet_ids}: {last_err}")


def wait_for_ssm_online(ssm_client: Any, instance_id: str, timeout_sec: int = 420) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        resp = ssm_client.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )
        infos = resp.get("InstanceInformationList", [])
        if infos and infos[0].get("PingStatus") == "Online":
            return
        time.sleep(8)
    raise TimeoutError(f"SSM agent did not come Online on {instance_id} within {timeout_sec}s")


def run_ssm_shell(ssm_client: Any, instance_id: str, script: str, timeout_sec: int = 1800) -> str:
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    resp = ssm_client.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        TimeoutSeconds=timeout_sec,
        Parameters={"commands": [f"echo {b64} | base64 -d | bash"]},
    )
    cmd_id = resp["Command"]["CommandId"]
    deadline = time.time() + timeout_sec + 60
    while time.time() < deadline:
        time.sleep(10)
        try:
            inv = ssm_client.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
        except ClientError:
            continue
        status = inv.get("Status", "")
        if status in ("Pending", "InProgress", "Delayed"):
            continue
        stdout = inv.get("StandardOutputContent", "")
        stderr = inv.get("StandardErrorContent", "")
        if status != "Success":
            raise RuntimeError(
                f"SSM command {cmd_id} failed ({status}):\nSTDOUT:\n{stdout[-3500:]}\nSTDERR:\n{stderr[-3500:]}"
            )
        return stdout
    raise TimeoutError(f"SSM command {cmd_id} timed out on {instance_id}")


def download_s3_prefix(s3_client: Any, bucket: str, prefix: str, local_dir: Path) -> list[Path]:
    local_dir.mkdir(parents=True, exist_ok=True)
    paginator = s3_client.get_paginator("list_objects_v2")
    downloaded: list[Path] = []
    norm_prefix = prefix.rstrip("/") + "/"
    for page in paginator.paginate(Bucket=bucket, Prefix=norm_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(norm_prefix) :]
            if not rel or rel.endswith("/"):
                continue
            dest = local_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3_client.download_file(bucket, key, str(dest))
            downloaded.append(dest)
    return downloaded


def terminate_and_verify_zero_leftovers(ec2_client: Any, instance_id: str | None) -> list[str]:
    if instance_id:
        try:
            ec2_client.terminate_instances(InstanceIds=[instance_id])
            waiter = ec2_client.get_waiter("instance_terminated")
            waiter.wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 5, "MaxAttempts": 120})
        except Exception:
            ec2_client.terminate_instances(InstanceIds=[instance_id])
            raise

    resp = ec2_client.describe_instances(
        Filters=[
            {"Name": "tag:Project", "Values": ["refactor-radar"]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ]
    )
    active: list[str] = []
    for res in resp.get("Reservations", []):
        for inst in res.get("Instances", []):
            active.append(f"{inst['InstanceId']}:{inst['State']['Name']}")
    return active


def query_spot_placement_scores(
    ec2_client: Any,
    instance_types: tuple[str, ...] = DEFAULT_INSTANCE_TYPES,
    region: str = DEFAULT_REGION,
) -> list[dict[str, Any]]:
    """Query AWS EC2 Spot placement scores via unified GPU burst orchestrator."""
    result = _orchestrator.evaluate_spot_placement_and_prices(
        ec2_client=ec2_client,
        instance_types=instance_types,
        target_capacity=1,
        region=region,
    )
    return list(result.get("placement_scores", []))


def build_runai_streamer_config(
    bucket: str,
    s3_prefix: str,
    model_id: str = "Qwen/Qwen3.5-4B-AWQ",
) -> dict[str, Any]:
    """Build vLLM runai_streamer S3 model streaming configuration via unified orchestrator."""
    cfg = _orchestrator.build_runai_streamer_config(bucket=bucket, s3_prefix=s3_prefix, model_id=model_id)
    return {
        "load_format": cfg["load_format"],
        "model_loader_extra_config": cfg["model_loader_extra_config"],
        "s3_model_uri": cfg["s3_model_uri"],
    }


def _build_spot_dry_run_summary(
    run_id: str,
    out_dir: Path,
    bucket: str,
    s3_prefix: str,
    profile: str,
    region: str,
) -> dict[str, Any]:
    _orchestrator.validate_u2_profile(profile)
    plan = _orchestrator.plan_gpu_burst(
        tier="large",
        region=region,
        profile=profile,
        nodes=1,
        bucket=bucket,
        dry_run=True,
        execute_approved_u2=False,
        check_aws=True,
        candidate_types=DEFAULT_INSTANCE_TYPES,
    )
    scores = list(plan["spot_placement"].get("placement_scores", []))
    streamer_cfg = build_runai_streamer_config(bucket, s3_prefix)
    run_out = out_dir / run_id
    run_out.mkdir(parents=True, exist_ok=True)
    summary = {
        "run_id": run_id,
        "dry_run": True,
        "default_state": plan["default_state"],
        "idle_stop_minutes": plan["idle_stop_minutes"],
        "instance_types": list(DEFAULT_INSTANCE_TYPES),
        "placement_scores": scores,
        "spot_placement": plan["spot_placement"],
        "quota_check": plan["quota_check"],
        "runai_streamer": streamer_cfg,
    }
    (run_out / "spot_run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def run_spot_batch(
    repos: list[tuple[str, Path]],
    out_dir: Path,
    profile: str = DEFAULT_PROFILE,
    region: str = DEFAULT_REGION,
    bucket: str = DEFAULT_BUCKET,
    spot_max_price: str = "2.25",
    with_scip: bool = False,
    auto_shutdown_minutes: int = 45,
    dry_run: bool = False,
    allow_low_sps: bool = False,
    candidate_types: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    _orchestrator.validate_u2_profile(profile)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    s3_prefix = f"refactor-radar/{run_id}"
    if dry_run:
        return _build_spot_dry_run_summary(run_id, out_dir, bucket, s3_prefix, profile, region)
    kit_root = Path(__file__).resolve().parents[3]

    session = boto3.Session(profile_name=profile, region_name=region)
    s3 = session.client("s3")
    ec2 = session.client("ec2")
    ssm = session.client("ssm")
    sq = session.client("service-quotas")

    base_candidates = candidate_types or (DEFAULT_EU_WEST_1_SPOT_TYPES if region == "eu-west-1" else DEFAULT_INSTANCE_TYPES)
    region_res = _orchestrator.resolve_region_instance_types(
        "large", region=region, ec2_client=ec2, candidate_types=base_candidates
    )
    resolved_types = tuple(region_res.get("selected_instance_types") or base_candidates)
    quota_check = _orchestrator.check_g_spot_quota(resolved_types, nodes=1, sq_client=sq, region=region)
    if not quota_check["passed"]:
        raise RuntimeError(f"Spot quota guard blocked run_spot_batch: {quota_check['diagnostic']}")
    launch_types = tuple(quota_check["valid_instance_types"])

    placement = _orchestrator.evaluate_spot_placement_and_prices(
        ec2_client=ec2,
        instance_types=launch_types,
        target_capacity=1,
        region=region,
    )
    if not placement["sps_gate_passed"] and not allow_low_sps:
        raise RuntimeError(f"Spot placement SPS gate blocked run_spot_batch: {placement['sps_diagnostic']}")

    stage_inputs_to_s3(s3, bucket, s3_prefix, repos, kit_root)

    instance_id: str | None = None
    launched_type = ""
    launched_subnet = ""
    remote_stdout = ""
    downloaded_files: list[Path] = []
    active_leftovers: list[str] = []

    try:
        instance_id, launched_type, launched_subnet = launch_one_time_spot_instance(
            ec2_client=ec2,
            ami_id=DEFAULT_AMI,
            subnet_ids=DEFAULT_PRIVATE_SUBNETS,
            sg_id=DEFAULT_SG,
            iam_profile_arn=DEFAULT_IAM_PROFILE,
            instance_types=launch_types,
            spot_max_price=spot_max_price,
            auto_shutdown_minutes=auto_shutdown_minutes,
            run_id=run_id,
        )
        ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
        wait_for_ssm_online(ssm, instance_id, timeout_sec=420)

        scip_flag = "--with-scip" if with_scip else "--no-with-scip"
        svc_list = " ".join(svc for svc, _ in repos)
        remote_script = f"""set -euo pipefail
WORK_ROOT="/opt/dlami/nvme/radar_{run_id}"
if [ ! -d "/opt/dlami/nvme" ]; then
  WORK_ROOT="/var/tmp/radar_{run_id}"
fi
mkdir -p "$WORK_ROOT/kit" "$WORK_ROOT/repos" "$WORK_ROOT/capsules" "$WORK_ROOT/hf_cache"
chmod -R 777 "$WORK_ROOT/hf_cache" "$WORK_ROOT/capsules"

aws s3 cp "s3://{bucket}/{s3_prefix}/input/kit.tar.gz" "$WORK_ROOT/kit.tar.gz" --only-show-errors
tar -xzf "$WORK_ROOT/kit.tar.gz" -C "$WORK_ROOT/kit"

for SVC in {svc_list}; do
  mkdir -p "$WORK_ROOT/repos/$SVC"
  aws s3 cp "s3://{bucket}/{s3_prefix}/input/repos/$SVC.tar.gz" "$WORK_ROOT/$SVC.tar.gz" --only-show-errors
  tar -xzf "$WORK_ROOT/$SVC.tar.gz" -C "$WORK_ROOT/repos/$SVC"
done

export RADAR_CACHE_DIR="$WORK_ROOT/hf_cache"
export RADAR_REPOS_DIR="$WORK_ROOT/repos"
export RADAR_CAPSULES_DIR="$WORK_ROOT/capsules"

CC="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -n1 | tr -d ' ' || echo '7.5')"
VRAM_MIB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -n1 | tr -d ' ' || echo '15360')"
GPU_COUNT="$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l | tr -d ' ' || echo '1')"
echo "[spot] GPU compute_cap=$CC vram_mib=$VRAM_MIB gpu_count=$GPU_COUNT"

if [ "$CC" = "7.5" ]; then
  export TEI_IMAGE="ghcr.io/huggingface/text-embeddings-inference:turing-1.5"
  export VLLM_DTYPE="float16"
  export VLLM_KV_DTYPE="float16"
elif [ "$CC" = "8.9" ]; then
  export TEI_IMAGE="ghcr.io/huggingface/text-embeddings-inference:89-1.5"
  export VLLM_DTYPE="bfloat16"
  export VLLM_KV_DTYPE="fp8_e4m3"
else
  export TEI_IMAGE="ghcr.io/huggingface/text-embeddings-inference:86-1.5"
  export VLLM_DTYPE="bfloat16"
  export VLLM_KV_DTYPE="auto"
fi

if [ "$GPU_COUNT" -ge 4 ] && [ "$VRAM_MIB" -ge 15000 ]; then
  export VLLM_TP="4"
  export LLM_MODEL="Qwen/Qwen3-32B-AWQ"
  export VLLM_MAX_LEN="16384"
  export VLLM_MEM_UTIL="0.80"
elif [ "$VRAM_MIB" -lt 20000 ]; then
  export VLLM_TP="1"
  export LLM_MODEL="Qwen/Qwen3.5-4B-AWQ"
  export VLLM_MAX_LEN="8192"
  export VLLM_MEM_UTIL="0.68"
else
  export VLLM_TP="1"
  export LLM_MODEL="Qwen/Qwen3-Coder-30B-A3B-Instruct-AWQ"
  export VLLM_MAX_LEN="12288"
  export VLLM_MEM_UTIL="0.70"
fi
export TEI_MODEL_ID="Qwen/Qwen3-Embedding-0.6B"

cd "$WORK_ROOT/kit/infra/refactor_radar"
docker compose up -d tei-embed vllm-coder
docker compose build analyzer

for i in $(seq 1 120); do
  if curl -sf http://127.0.0.1:8081/health >/dev/null && curl -sf http://127.0.0.1:8000/v1/models >/dev/null; then
    echo "[spot] TEI + vLLM healthy after $((i * 5))s"
    break
  fi
  if [ "$i" -eq 120 ]; then
    docker compose logs --tail 60 tei-embed vllm-coder || true
    exit 1
  fi
  sleep 5
done

for SVC in {svc_list}; do
  echo "[spot] Analyzing $SVC ..."
  docker compose run --rm analyzer analyze \\
    --repo "/repos/$SVC" \\
    --service-name "$SVC" \\
    --capsule-out "/capsules/$SVC" \\
    {scip_flag} \\
    --embed-backend tei
done

echo "[spot] Running Phase 2 Federation ..."
docker compose run --rm analyzer federate \\
  --capsules-root /capsules \\
  --out-dir /capsules/_federated

docker compose down -v || true
aws s3 sync "$WORK_ROOT/capsules" "s3://{bucket}/{s3_prefix}/capsules/" --only-show-errors
echo "[spot] S3 sync completed."
"""
        remote_stdout = run_ssm_shell(ssm, instance_id, remote_script, timeout_sec=1800)
        run_out_dir = out_dir / run_id
        downloaded_files = download_s3_prefix(s3, bucket, f"{s3_prefix}/capsules", run_out_dir)
    finally:
        active_leftovers = terminate_and_verify_zero_leftovers(ec2, instance_id)

    summary = {
        "run_id": run_id,
        "instance_id": instance_id,
        "instance_type": launched_type,
        "subnet_id": launched_subnet,
        "s3_capsules_uri": f"s3://{bucket}/{s3_prefix}/capsules/",
        "local_output_dir": str((out_dir / run_id).resolve()),
        "downloaded_count": len(downloaded_files),
        "active_leftover_instances": active_leftovers,
        "remote_stdout_tail": remote_stdout[-2500:],
    }
    (out_dir / run_id / "spot_run_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return summary
