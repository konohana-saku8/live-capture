# -*- coding: utf-8 -*-
"""单片任务的状态机：下载 → 试算 → 编码 → 校验 → 上传 → 提交。

两条原则：
- 任何一步失败，手机上的原分片一个字节都不动
- 试算觉得不划算就跳过，不浪费传输和编码时间
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field

from . import config, encoder, probe, verify
from .api import Aborted, ApiError, Client, human_size

SAMPLE_SECONDS = 30
MIN_SAVING = 0.15          # 预计只省 15% 以内就别折腾了


@dataclass
class Part:
    rec_id: int
    idx: int
    label: str
    path: str
    bytes: int
    duration_ms: int
    started_at: int = 0          # 本场开播时间（毫秒），用来区分是哪一场
    status: str = "pending"
    message: str = ""
    progress: float = 0.0
    info: dict = field(default_factory=dict)
    est_bytes: int = 0
    ssim: float = -1.0
    saved: int = 0

    @property
    def kbps(self) -> int:
        if not self.duration_ms:
            return 0
        return int(self.bytes * 8 / (self.duration_ms / 1000.0) / 1000)

    @property
    def when(self) -> str:
        if not self.started_at:
            return self.label or str(self.rec_id)
        return time.strftime("%m-%d %H:%M", time.localtime(self.started_at / 1000.0))

    @property
    def title(self) -> str:
        secs = int(self.duration_ms / 1000)
        return "%s 第%d片 · %d:%02d · %d kbps · %s" % (
            self.when, self.idx + 1, secs // 60, secs % 60,
            self.kbps, human_size(self.bytes))

    @property
    def saving_ratio(self) -> float:
        if not self.est_bytes or not self.bytes:
            return 0.0
        return max(0.0, 1.0 - self.est_bytes / float(self.bytes))


def scan(client: Client, min_kbps: int = 0, min_bytes: int = 0) -> list[Part]:
    """把录像列表展开成「一片一行」，顺手标出值不值得压。

    断流重连会留下几秒的碎片（几百 KB 到几 MB），码率可能很高，但压完只省
    几百 KB，却要占用一次完整的下载-编码-回传。``min_bytes`` 用来挡掉它们。
    """
    parts: list[Part] = []
    for rec in client.recordings():
        if (rec.get("status") or "").lower() != "ready":
            continue
        for idx, item in enumerate(rec.get("mp4_parts") or []):
            p = Part(rec_id=int(rec.get("id") or 0), idx=idx,
                     label=rec.get("label") or "",
                     path=item.get("path") or "",
                     bytes=int(item.get("bytes") or 0),
                     duration_ms=int(item.get("duration_ms") or 0),
                     started_at=int(rec.get("started_at") or 0))
            if min_bytes and p.bytes < min_bytes:
                p.status = "skip"
                p.message = "只有 %s，压了省不下多少" % human_size(p.bytes)
            elif min_kbps and p.kbps and p.kbps < min_kbps:
                p.status = "skip"
                p.message = "码率 %d kbps，压了也省不下多少" % p.kbps
            parts.append(p)
    return parts


class Session:
    """一次「扫描 → 处理」的上下文，GUI 和 CLI 都通过它干活。"""

    def __init__(self, cfg: dict, client: Client, log=None):
        self.cfg = cfg
        self.client = client
        self._log = log or (lambda level, text: None)
        self.ffmpeg, self.ffprobe = config.find_ffmpeg(cfg)
        self.encoders = encoder.detect(self.ffmpeg)
        self.kind = encoder.pick(self.encoders, str(cfg.get("encoder") or "auto"))
        self.work = str(cfg.get("work_dir") or config.DEFAULTS["work_dir"])
        self.stopped = False
        self._holder: dict = {}

    # ---------- 基础 ----------

    def log(self, level: str, text: str) -> None:
        self._log(level, text)

    @property
    def ready(self) -> bool:
        return bool(self.ffmpeg and self.encoders)

    def describe(self) -> str:
        if not self.ffmpeg:
            return "没找到 ffmpeg。" + config.vendor_hint()
        if not self.encoders:
            return "ffmpeg 里没有可用的 h264 编码器，换一个完整 build"
        return "%s（%s）" % (self.encoders[0], encoder.LABELS.get(self.kind, ""))

    def _dir(self, part: Part) -> str:
        d = os.path.join(self.work, str(part.rec_id), "p%d" % part.idx)
        os.makedirs(d, exist_ok=True)
        return d

    # ---------- 主流程 ----------

    def run(self, part: Part, on_step=None, dry: bool = False) -> Part:
        """跑完一片的完整链路；``dry`` 只下载 + 试算，不编码也不回传。"""
        def step(name: str, ratio: float = -1.0):
            part.status = name
            if ratio >= 0:
                part.progress = ratio
            if on_step:
                on_step(part, name, ratio)

        try:
            if not self.ready:
                raise RuntimeError(self.describe())
            if part.status == "skip":
                return part

            d = self._dir(part)
            src = os.path.join(d, "src.mp4")
            enc = os.path.join(d, "enc.mp4")

            step("下载", 0.0)
            self.client.download(part.rec_id, part.idx, src,
                                 on_progress=lambda a, b: on_step and on_step(part, "下载", a / b if b else 0),
                                 stop=lambda: self.stopped)
            part.info = probe.probe(self.ffprobe, src)
            self.log("info", "%s：%s，源 %s" % (
                part.title, probe.summary(part.info), human_size(part.info["bytes"])))

            # 试算：真按这个参数压 30 秒，外推全片，避免「压完发现更大」
            if part.info["duration_ms"] > SAMPLE_SECONDS * 2 * 1000:
                step("试算", 0.0)
                sample = os.path.join(d, "sample.mp4")
                start = max(0, part.info["duration_ms"] // 2000 - SAMPLE_SECONDS * 500)
                cmd = encoder.sample_cmd(
                    self.ffmpeg, src, sample, start=start // 1000,
                    seconds=SAMPLE_SECONDS, encoder=self.kind,
                    crf=int(self.cfg["crf"]),
                    max_bitrate=int(self.cfg["max_bitrate"]),
                    audio_bitrate=int(self.cfg["audio_bitrate"]))
                code, err = encoder.run(cmd, SAMPLE_SECONDS * 1000, None)
                if code != 0:
                    raise RuntimeError("试算失败：%s" % (err.splitlines()[-1][:160] if err else code))
                got = os.path.getsize(sample)
                try:
                    os.remove(sample)
                except OSError:
                    pass
                part.est_bytes = int(got * part.info["duration_ms"] / (SAMPLE_SECONDS * 1000.0))
                if part.saving_ratio < MIN_SAVING:
                    part.status = "skip"
                    part.message = "试算只能省 %.0f%%，不压" % (part.saving_ratio * 100)
                    self.log("warn", "%s：%s" % (part.title, part.message))
                    self._cleanup(d, keep_src=True)
                    return part
                self.log("info", "%s：预计 %.1f MB → %.1f MB（省 %.0f%%）" % (
                    part.title, part.bytes / 1048576.0,
                    part.est_bytes / 1048576.0, part.saving_ratio * 100))

            if dry:
                part.status = "estimated"
                if part.est_bytes:
                    part.message = "预计 %.1f MB（省 %.0f%%）" % (
                        part.est_bytes / 1048576.0, part.saving_ratio * 100)
                else:
                    part.message = "片段太短，未试算"
                self.log("info", "%s：%s" % (part.title, part.message))
                self._cleanup(d, keep_src=False)
                return part

            step("编码", 0.0)
            cmd = encoder.build(self.ffmpeg, src, enc, encoder=self.kind,
                                crf=int(self.cfg["crf"]),
                                max_bitrate=int(self.cfg["max_bitrate"]),
                                audio_bitrate=int(self.cfg["audio_bitrate"]))
            code, err = encoder.run(
                cmd, part.info["duration_ms"],
                lambda p: on_step and on_step(part, "编码", p), self._holder)
            if self.stopped:
                raise Aborted("已停止")
            if code != 0:
                raise RuntimeError("编码失败：%s" % (err.splitlines()[-1][:200] if err else code))

            step("校验", 0.0)
            report = verify.verify(
                self.ffmpeg, src, enc,
                expect_duration_ms=part.info["duration_ms"],
                expect_frames=part.info.get("frames") or 0,
                check_ssim=bool(self.cfg.get("check_ssim", True)),
                on_stage=lambda name: on_step and on_step(part, "校验·" + name, -1))
            part.ssim = report.get("ssim", -1.0)
            if not report["ok"]:
                raise RuntimeError("校验不过：" + "；".join(report["issues"]))
            new_size = os.path.getsize(enc)
            if new_size >= part.bytes:
                part.status = "skip"
                part.message = "压完 %.1f MB 反而更大" % (new_size / 1048576.0)
                self.log("warn", "%s：%s" % (part.title, part.message))
                self._cleanup(d, keep_src=True)
                return part

            step("上传", 0.0)
            done = self.client.upload(
                part.rec_id, part.idx, enc,
                chunk_mb=int(self.cfg.get("chunk_mb", 8)),
                on_progress=lambda a, b: on_step and on_step(part, "上传", a / b if b else 0),
                stop=lambda: self.stopped)
            part.saved = int(done.get("saved_bytes") or (part.bytes - new_size))
            part.status = "done"
            part.message = "省 %.1f MB（SSIM %.3f）" % (part.saved / 1048576.0, part.ssim)
            self.log("info", "%s：完成，%s" % (part.title, part.message))
            self._archive(d, part)
            return part

        except Aborted as exc:
            part.status = "stopped"
            part.message = str(exc)
            self.log("warn", "%s：%s" % (part.title, str(exc)))
            return part
        except (ApiError, RuntimeError, OSError) as exc:
            part.status = "failed"
            part.message = str(exc)
            self.log("error", "%s：%s" % (part.title, exc))
            return part

    # ---------- 收尾 ----------

    def stop(self) -> None:
        self.stopped = True
        encoder.stop(self._holder)

    def _archive(self, folder: str, part: Part) -> None:
        """压好的原件按天留一份，误删了能反向上传回去。"""
        keep = int(self.cfg.get("keep_days", 7) or 0)
        src = os.path.join(folder, "src.mp4")
        if keep <= 0 or not os.path.exists(src):
            self._cleanup(folder, keep_src=False)
            return
        dst_dir = os.path.join(self.work, "archive", time.strftime("%Y-%m-%d"))
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, "%d_p%d.mp4" % (part.rec_id, part.idx))
        try:
            os.replace(src, dst)
        except OSError:
            pass
        self._cleanup(folder, keep_src=False)

    def _cleanup(self, folder: str, keep_src: bool) -> None:
        for name in ("enc.mp4", "sample.mp4"):
            path = os.path.join(folder, name)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        if not keep_src:
            try:
                shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass

    def prune_archive(self) -> None:
        """删掉超过 keep_days 的原件目录。"""
        keep = int(self.cfg.get("keep_days", 7) or 0)
        root = os.path.join(self.work, "archive")
        if keep <= 0 or not os.path.isdir(root):
            return
        limit = time.time() - keep * 86400
        for name in os.listdir(root):
            path = os.path.join(root, name)
            try:
                if os.path.isdir(path) and os.path.getmtime(path) < limit:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue
