"""stage_node：把本地文件变成「可下载 URL」，供 OCR 网关拉取。

backend=local：复制到 staging 目录 + 后台 HTTP 文件服务器（零依赖，本地联调）。
backend=s3   ：上传 MinIO/S3 并生成 presigned GET URL。
backend=oss  ：上传阿里云 OSS 并生成原生签名 URL（国内公网可达，生产推荐）。
"""
from __future__ import annotations

import functools
import re
import shutil
import threading
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from bill_agent.config import config

_server: ThreadingHTTPServer | None = None
_server_lock = threading.Lock()


class _QuietHandler(SimpleHTTPRequestHandler):
    """只读、不可列举的临时文件服务（可安全暴露到公网）。"""

    def log_message(self, *args):  # noqa: D102
        pass

    def list_directory(self, path):  # noqa: D102 - 关闭目录列举，只能按完整 URL 取文件
        self.send_error(404, "Not Found")
        return None


def _ensure_local_server(directory: str, host: str, port: int) -> str:
    """惰性启动一个服务于 staging 目录的后台 HTTP 服务器，返回 base_url。"""
    global _server
    with _server_lock:
        if _server is None:
            handler = functools.partial(_QuietHandler, directory=str(directory))
            _server = ThreadingHTTPServer((host, port), handler)
            threading.Thread(target=_server.serve_forever, daemon=True).start()
    return f"http://{host}:{port}"


def _stage_local_http(path: str) -> str:
    stage = config.stage
    local = stage.get("local", {})
    host = local.get("host", "127.0.0.1")
    port = int(local.get("port", 8787))
    directory = local.get("dir", ".staging")

    staging_dir = Path(directory)
    staging_dir.mkdir(parents=True, exist_ok=True)

    # 随机前缀：公网暴露时 URL 不可猜（目录列举已在 handler 中关闭）
    dest = staging_dir / _object_name(path)
    shutil.copyfile(path, dest)

    # 本地服务器仍需运行（公网地址通常反向代理到它）
    _ensure_local_server(directory, host, port)

    # 返回网关可达的地址；未配置公网地址时只能返回本机地址（网关会 504）
    base = config.stage_public_base_url or f"http://{host}:{port}"
    return f"{base}/{Path(dest.name).as_posix()}"


def _s3_settings() -> dict[str, Any]:
    """S3 配置；凭据优先取环境变量（.env），避免明文落在 YAML 里。"""
    import os

    s3 = dict(config.stage.get("s3", {}))
    s3["access_key"] = os.getenv("MINIO_ROOT_USER") or s3.get("access_key", "minioadmin")
    s3["secret_key"] = os.getenv("MINIO_ROOT_PASSWORD") or s3.get("secret_key", "")
    return s3


def _public_s3_endpoint() -> str:
    """网关侧访问用的 endpoint；未配置时退回本机 endpoint。"""
    s3 = _s3_settings()
    return str(s3.get("public_endpoint") or s3.get("endpoint") or "http://localhost:9000")


def _object_name(path: str) -> str:
    """对象名用「短哈希 + 安全文件名」，避开中文/空格/不间断空格的 URL 编码问题。"""
    name = Path(path).name.replace("\xa0", " ")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[-80:]
    return f"{uuid.uuid4().hex[:8]}_{safe}"


def _s3_client(endpoint_url: str, s3: dict[str, Any]):
    """按 endpoint 造一个 MinIO 客户端（secure 由 scheme 决定）。"""
    from minio import Minio

    secure = endpoint_url.startswith("https://")
    host = endpoint_url.split("://", 1)[-1].rstrip("/")
    return Minio(host, s3["access_key"], s3["secret_key"], secure=secure)


def _stage_s3(path: str) -> str:
    """上传到 S3/MinIO 并返回预签名 GET URL（无需把桶设为公开）。

    上传走本机 endpoint（快、不经过公网），只有预签名 URL 指向公网 endpoint；
    签名只依赖 host，因此网关拿到的 URL 与本机上传互不影响。
    """
    from datetime import timedelta

    s3 = _s3_settings()
    bucket = s3.get("bucket", "bill-files")
    local_url = s3.get("endpoint") or _public_s3_endpoint()

    uploader = _s3_client(local_url, s3)
    object_name = _object_name(path)
    if not uploader.bucket_exists(bucket):
        uploader.make_bucket(bucket)
    uploader.fput_object(bucket, object_name, path)

    signer = _s3_client(_public_s3_endpoint(), s3)
    hours = int(s3.get("presign_expiry_hours", 24))
    return signer.presigned_get_object(bucket, object_name, expires=timedelta(hours=hours))


def _oss_settings() -> dict[str, Any]:
    """OSS 配置；凭据优先取环境变量（.env），避免明文落在 YAML 里。"""
    import os

    oss = dict(config.stage.get("oss", {}))
    oss["access_key"] = os.getenv("OSS_ACCESS_KEY_ID") or oss.get("access_key", "")
    oss["secret_key"] = os.getenv("OSS_ACCESS_KEY_SECRET") or oss.get("secret_key", "")
    return oss


def _stage_oss(path: str) -> str:
    """上传到阿里云 OSS 并返回原生签名 URL（SigV2，OSS 全区域支持）。

    与 s3 后端不同：OSS 上传与取件是同一个国内公网 endpoint，无本机快传路径。
    """
    import oss2

    oss = _oss_settings()
    endpoint = str(oss.get("endpoint") or "").rstrip("/")
    if endpoint and "://" not in endpoint:
        endpoint = "https://" + endpoint  # oss2 对无 scheme 的 endpoint 默认补 http://，明文不安全
    bucket_name = str(oss.get("bucket") or "bill-files")
    if not endpoint:
        raise RuntimeError("oss.endpoint 未配置（settings.yaml 的 stage.oss.endpoint）")
    if not oss["access_key"] or not oss["secret_key"]:
        raise RuntimeError(
            "OSS 凭据未配置：.env 的 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET"
            "（或 settings.yaml 的 stage.oss.access_key / secret_key）"
        )

    auth = oss2.Auth(oss["access_key"], oss["secret_key"])
    bucket = oss2.Bucket(auth, endpoint, bucket_name)
    object_name = _object_name(path)
    try:
        bucket.put_object_from_file(object_name, path)
    except oss2.exceptions.NoSuchBucket as e:
        raise RuntimeError(
            f"OSS 桶不存在：{bucket_name}（请先在阿里云控制台创建，或在 settings.yaml 改 bucket 名）"
        ) from e
    except oss2.exceptions.AccessDenied as e:
        raise RuntimeError(
            f"OSS 权限不足：当前 RAM 用户对桶 {bucket_name} 没有 oss:PutObject 权限"
            "（请在阿里云 RAM 控制台检查该用户的授权策略）"
        ) from e

    hours = int(oss.get("presign_expiry_hours", 24))
    return bucket.sign_url("GET", object_name, hours * 3600)  # expires 单位：秒


def stage_local_file(path: str) -> str:
    """入口：根据配置 backend 选择本地 HTTP / S3 / OSS，返回可下载 URL。"""
    backend = config.stage.get("backend", "local")
    if backend == "oss":
        return _stage_oss(path)
    if backend == "s3":
        return _stage_s3(path)
    return _stage_local_http(path)


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    raw = url if "://" in url else f"http://{url}"
    return (urlparse(raw).hostname or "").lower()


def is_public_host(url_or_host: str) -> bool:
    """判断地址是否是公网可达主机。

    回环 / RFC1918 / 链路本地 / 保留地址一律视为不可达——
    OCR 网关在公网，拉不到 192.168.x.x（实测 60s 后 504）。
    """
    import ipaddress

    host = _host_of(url_or_host)
    if not host or host == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # 域名交给 DNS，视为公网（内网域名需自行保证可达）
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified)


def ocr_delivery_blocker() -> str | None:
    """OCR 不可用的具体原因（None = 可用），供错误消息与审计提示使用。

    只描述配置缺口，不回显任何凭据值。就绪判定与错误提示共用这一处：
    否则「OSS 凭据没填」会被笼统报成「staging 地址不可达」，把用户引向
    错误的方向（round 14）。
    """
    stage = config.stage
    backend = stage.get("backend", "local")
    if backend == "s3":
        endpoint = _public_s3_endpoint()
        if not is_public_host(endpoint):
            host = _host_of(endpoint) or "未配置"
            return f"stage.s3.public_endpoint 不是国内公网地址（{host}），OCR 网关无法拉取文件"
        return None
    if backend == "oss":
        oss = _oss_settings()
        endpoint = str(oss.get("endpoint") or "")
        if not endpoint:
            return "stage.oss.endpoint 未配置（config/settings.yaml）"
        if not is_public_host(endpoint):
            return f"stage.oss.endpoint 不是国内公网地址（{_host_of(endpoint)}）"
        missing = [
            env
            for env, key in (
                ("OSS_ACCESS_KEY_ID", "access_key"),
                ("OSS_ACCESS_KEY_SECRET", "secret_key"),
            )
            if not oss.get(key)
        ]
        if missing:
            return "OSS 凭据未配置：.env 的 " + " / ".join(missing)
        return None
    base = config.stage_public_base_url
    if not base:
        return "stage.local.public_base_url 未配置（config/settings.yaml）"
    if not is_public_host(base):
        return f"stage.local.public_base_url 不是国内公网地址（{_host_of(base)}）"
    return None


def ocr_delivery_ready() -> bool:
    """OCR 网关能否真的拉到 staged 文件（决定 OCR 是否可用）。

    local 后端必须有公网 public_base_url；s3 后端的 public_endpoint、
    oss 后端的 endpoint 必须国内公网可达（.aliyuncs.com 域名即满足）。
    """
    return ocr_delivery_blocker() is None
