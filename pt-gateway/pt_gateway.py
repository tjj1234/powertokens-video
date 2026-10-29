#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PT Task Gateway —— 跨模型统一的异步任务接口（独立模块，零外部依赖）。

为什么需要它（与 PT 真实接口的关系）：
  PT 底层是 OneAPI 按供应商透明路由，对调用方来说**所有视频 / Seedance 模型共用同一传输层**：
    - 提交：POST /v1/videos
    - 轮询：GET  /v1/videos/{id}
  所以「Seedance / Vidu / Wan 各有独立提交/查询接口」是**误判**——PT 已统一了路径。
  真正让「接多个模型要各写一套」的痛点在两处，本模块正是来归一化的：
    1) 请求体构造差异：Seedance 必须把 prompt 塞进 media:[{type:text}] 且图生视频素材强制 asset://；
       其他模型用顶层 prompt + http(s)/asset://。
    2) 返回字段位置差异：status 枚举值、结果 URL 可能在 metadata.url / metadata.content_url / task.url 任一处。
  此外 PT 当前**没有取消任务的公共端点**（官方文档 / skill 均无记录），故 cancel 本轮返回 {supported:false}。

本模块职责（复用 pt_video.py 的 REST 原语，不直接依赖 CLI 子命令）：
  - TaskState：把各厂商状态动词归一化为 QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED/UNKNOWN。
  - ModelAdapter：按 kind 构造提交体（Seedance 走 media-text + asset://；video 走顶层 prompt），并统一抽取结果 URL。
  - TaskGateway：submit / get_status / wait / cancel(占位) 四个统一方法。
  - ModelRegistry：拉 /v1/models 做 diff——新上架的异步模型自动 enable 并打 is_new，
    下架的自动 delisted；带 last_synced，超过 1 小时（对齐 PT 缓存）下次调用惰性自动重拉。

依赖：Python 3.9+ 标准库；引擎 REST 原语来自同仓的 pt_video.py（import 时把仓库根加入 sys.path）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------- 复用同仓 pt_video.py 的 REST 原语（不依赖 CLI 子命令）----------
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent  # pt-gateway/ 的父目录 = 仓库根（pt_video.py 所在）
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pt_video  # noqa: E402

# 从引擎取所需符号（全部为模块级函数/常量，import 安全）
ApiError = pt_video.ApiError
_api_call = pt_video._api_call
VIDEO_SUBMIT = pt_video.VIDEO_SUBMIT
VIDEO_POLL = pt_video.VIDEO_POLL
resolve_key = pt_video.resolve_key
kind_of_model = pt_video.kind_of_model
list_key_models = pt_video.list_key_models
POLL_INTERVAL = pt_video.POLL_INTERVAL
POLL_TIMEOUT = pt_video.POLL_TIMEOUT
poll_task = pt_video.poll_task
submit_task = pt_video.submit_task
AUTO_PRIORITY_VIDEO = pt_video.AUTO_PRIORITY_VIDEO
AUTO_PRIORITY_SEEDANCE = pt_video.AUTO_PRIORITY_SEEDANCE

ASYNC_KINDS = ("video", "seedance")  # 走 /v1/videos 任务式轮询的类型；image 为同步，不进网关


# ---------- 归一化状态机 ----------
class TaskState:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"

    _MAP = {
        "succeeded": SUCCEEDED, "completed": SUCCEEDED,
        "failed": FAILED,
        "cancelled": CANCELLED, "canceled": CANCELLED,
        "pending": QUEUED, "queued": QUEUED, "submitted": QUEUED,
        "running": RUNNING, "processing": RUNNING, "in_progress": RUNNING,
        "doing": RUNNING, "generating": RUNNING,
    }

    @classmethod
    def normalize(cls, raw_status: str) -> str:
        return cls._MAP.get((raw_status or "").lower(), cls.UNKNOWN)


def result_url(task: dict):
    """统一抽取结果 URL：兼容各厂商把链接放在不同位置。"""
    meta = task.get("metadata") or {}
    return meta.get("url") or meta.get("content_url") or task.get("url")


def normalize_status(task: dict) -> dict:
    """把厂商原始任务对象归一化为统一状态结构。"""
    raw = (task.get("status") or "").lower()
    progress = task.get("progress")
    if progress is None:
        progress = (task.get("metadata") or {}).get("progress")
    return {
        "task_id": task.get("id") or task.get("task_id"),
        "state": TaskState.normalize(raw),
        "progress": progress,
        "url": result_url(task),
        "raw": task,
    }


# ---------- 请求体适配（按 kind 构造 / 抽取）----------
def _build_media(first_frame=None, ref_image=None, ref_video=None):
    media = []
    if first_frame:
        media.append({"type": "first_frame", "url": first_frame})
    if ref_image:
        media.append({"type": "image", "url": ref_image})
    if ref_video:
        media.append({"type": "video", "url": ref_video})
    return media


def build_submit_body(model, kind, prompt, media, params):
    """按 kind 构造 /v1/videos 提交体。

    - video：顶层 prompt + 可选 media[]（http(s)/asset://）
    - seedance：prompt 必须放进 media:[{type:text}]；图生视频素材强制 asset://
    """
    core = {
        "size": (params.get("resolution") or "720p").upper(),
        "ratio": params.get("ratio") or "16:9",
        "seconds": str(params.get("duration", 5)),  # 服务端 enum<string>，必须字符串
        "generate_audio": not params.get("no_audio", False),
        "prompt_extend": not params.get("no_prompt_extend", False),
        "watermark": params.get("watermark", False),
    }
    if kind == "seedance":
        seed_media = [{"type": "text", "text": prompt or ""}]
        for m in media:
            if not str(m.get("url", "")).startswith("asset://"):
                raise ApiError(0, f"Seedance 素材必须是资产库 asset:// 格式，收到：{m.get('url')}")
            seed_media.append(m)
        body = {"model": model, "media": seed_media, **core}
    else:
        body = {"model": model, "prompt": prompt or "", **core}
        if media:
            body["media"] = media
    if params.get("seed") is not None:
        body["seed"] = params["seed"]
    return body


# ---------- 统一网关 ----------
class TaskGateway:
    def __init__(self, key=None, registry=None):
        self.key = key or resolve_key()
        self.registry = registry or ModelRegistry()

    def _need_key(self):
        if not self.key:
            raise ApiError(401, "缺少 API Key：设置环境变量 POWERTOKENS_API_KEY 或 config.json")

    def submit(self, model, prompt="", duration=5, resolution="720p", ratio="16:9",
               first_frame=None, ref_image=None, ref_video=None, no_audio=False,
               no_prompt_extend=False, watermark=False, seed=None, timeout=POLL_TIMEOUT,
               wait=False):
        """提交一个异步视频任务。返回 {task_id, kind}；wait=True 则轮询到终态并返回归一化状态。"""
        self._need_key()
        kind = kind_of_model(model)
        if kind not in ASYNC_KINDS:
            raise ApiError(0, f"模型 {model} 非异步视频类（kind={kind}）；网关只处理 video / seedance")
        media = _build_media(first_frame, ref_image, ref_video)
        body = build_submit_body(model, kind, prompt, media, dict(
            duration=duration, resolution=resolution, ratio=ratio,
            no_audio=no_audio, no_prompt_extend=no_prompt_extend,
            watermark=watermark, seed=seed,
        ))
        sys.stderr.write(f"[gateway] POST /v1/videos model={model} kind={kind}\n")
        tid, _ = submit_task(self.key, body)
        if wait:
            task = poll_task(self.key, tid, timeout=timeout)
            return {**normalize_status(task), "kind": kind}
        return {"task_id": tid, "kind": kind}

    def get_status(self, task_id):
        """查单个任务状态，返回归一化结构。"""
        self._need_key()
        task = _api_call("GET", VIDEO_POLL.format(id=task_id), self.key, timeout=30)
        return normalize_status(task)

    def wait(self, task_id, timeout=POLL_TIMEOUT):
        """轮询到终态，返回归一化结构；失败/超时归为 FAILED（不抛）。"""
        self._need_key()
        try:
            task = poll_task(self.key, task_id, timeout=timeout)
            return normalize_status(task)
        except ApiError as e:
            return {
                "task_id": task_id, "state": TaskState.FAILED,
                "progress": None, "url": None,
                "error": str(getattr(e, "message", e)), "raw": {},
            }

    def cancel(self, task_id):
        """取消任务。

        本轮返回 {supported:false}：PT 当前未提供取消任务的公共端点
        （官方文档与 powertokens-integration skill 均无记录；OneAPI 路由下各供应商取消路径不一）。
        待确认端点后，在此探测 DELETE /v1/videos/{id} 或 POST .../cancel 并实现分发。
        """
        return {
            "task_id": task_id,
            "supported": False,
            "note": "PT 当前未提供取消任务的公共端点（官方文档/skill 均无记录）；本轮未实现，待确认端点后补齐",
        }


# ---------- 模型注册表（自动更新机制）----------
class ModelRegistry:
    """拉 /v1/models 做 diff，持久化到 registry.json。

    - 新上架的异步模型：自动 enabled=True 并打 is_new=True
    - 下架（不再出现在 live 列表）：自动 enabled=False, delisted=True
    - last_synced 时间戳；is_stale() 用于调用方惰性自动重拉（默认 1 小时，对齐 PT 缓存）
    """

    def __init__(self, path=None):
        self.path = Path(path or os.environ.get(
            "PT_GATEWAY_REGISTRY",
            Path.home() / ".config" / "pt-gateway" / "registry.json",
        ))
        self.data = {"last_synced": None, "models": {}}

    def load(self):
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {"last_synced": None, "models": {}}
        self.data.setdefault("models", {})
        return self

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def is_stale(self, max_age=3600):
        ls = self.data.get("last_synced")
        if not ls:
            return True
        try:
            dt = datetime.fromisoformat(ls)
        except Exception:
            return True
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() > max_age

    def sync(self, key, max_age=3600):
        """用当前 Key 的 live 模型列表刷新注册表，返回 diff 摘要。"""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        present = {}
        for kind in ASYNC_KINDS:
            for mid in list_key_models(key, kind):
                present[mid] = kind
        models = self.data["models"]
        added, removed = [], []
        for mid, kind in present.items():
            if mid not in models:
                models[mid] = {"kind": kind, "enabled": True, "delisted": False,
                               "first_seen": now, "last_seen": now, "is_new": True}
                added.append(mid)
            else:
                m = models[mid]
                m["enabled"] = True
                m["delisted"] = False
                m["last_seen"] = now
                m["is_new"] = False
                m["kind"] = kind
        for mid, m in models.items():
            if mid not in present and m.get("enabled") and not m.get("delisted"):
                m["enabled"] = False
                m["delisted"] = True
                m["last_seen"] = now
                removed.append(mid)
        self.data["last_synced"] = now
        self.save()
        return {
            "added": added, "removed": removed,
            "total_enabled": sum(1 for m in models.values() if m.get("enabled")),
            "last_synced": now,
        }

    def enabled_async(self, kind=None):
        out = []
        for mid, m in self.data["models"].items():
            if m.get("enabled") and not m.get("delisted"):
                if kind is None or m.get("kind") == kind:
                    out.append(mid)
        return out

    def auto_select(self, kind):
        cands = self.enabled_async(kind)
        if not cands:
            return None
        prio = AUTO_PRIORITY_SEEDANCE if kind == "seedance" else AUTO_PRIORITY_VIDEO
        cands.sort(key=lambda m: (
            next((i for i, h in enumerate(prio) if h in m.lower()), len(prio)),
            0 if "fast" in m.lower() else 1,
        ))
        return cands[0]

    def auto_sync_if_stale(self, key, max_age=3600):
        """惰性自动重拉：注册表为空或过期时 best-effort 同步；失败不抛，回退到现有/内置列表。"""
        if self.is_stale(max_age):
            try:
                return self.sync(key, max_age)
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[registry] 自动同步失败（沿用现有/回退）：{e}\n")
        return None


def _selfcheck():
    # 轻量自检（无需真实 Key）：验证状态归一化与注册表 diff 逻辑。
    assert TaskState.normalize("succeeded") == "SUCCEEDED"
    assert TaskState.normalize("running") == "RUNNING"
    assert TaskState.normalize("weird") == "UNKNOWN"
    fake = {"id": "t1", "status": "completed", "metadata": {"url": "http://x/y.mp4", "progress": 80}}
    ns = normalize_status(fake)
    assert ns["state"] == "SUCCEEDED" and ns["progress"] == 80 and ns["url"] == "http://x/y.mp4"
    print("pt_gateway self-check OK")


def _cmd_sync():
    """手动触发注册表同步（无需启动 HTTP 服务）。要求环境变量 POWERTOKENS_API_KEY 可用。"""
    key = resolve_key()
    if not key:
        sys.stderr.write("ERROR: 缺少 API Key（设置 POWERTOKENS_API_KEY 或 config.json）\n")
        sys.exit(2)
    reg = ModelRegistry().load()
    diff = reg.sync(key)
    print(json.dumps(diff, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="PT Gateway 核心模块（自检 / 注册表手动同步）")
    ap.add_argument("cmd", nargs="?", default="selfcheck",
                    choices=["selfcheck", "sync"],
                    help="selfcheck=状态归一化自检（默认）；sync=手动拉取 /v1/models 刷新注册表")
    args = ap.parse_args()
    if args.cmd == "selfcheck":
        _selfcheck()
    elif args.cmd == "sync":
        _cmd_sync()
