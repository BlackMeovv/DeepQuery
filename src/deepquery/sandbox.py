"""图表代码沙箱：执行模型生成的 Python 画图代码。

模型生成的代码是不受信内容，绝不允许在主进程里 exec。两种执行器：
- DockerSandbox（生产首选）：--network none 断网 + 内存/CPU 限额 + 只读挂载工作目录，
  镜像见 docker/chart-sandbox/Dockerfile；
- SubprocessSandbox（开发兜底 / 容器内运行时）：独立子进程 + resource 限额
  （地址空间/CPU 时间/文件大小）+ 隔离模式 python -I + 清空代理环境变量。
  注意它不隔离网络与文件系统，安全性弱于 Docker——仅用于本机开发或
  自身已跑在容器里的场景（compose 里 app 容器整体就是隔离边界）。

代码契约（写进提示词）：工作目录有 data.json（{"columns": [...], "rows": [...]})，
代码读取它并把图保存为 chart.png；只允许用 matplotlib/标准库。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


# 完整 8 字节 PNG 签名；产物上限 10MB（正常图表几十到几百 KB）
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MAX_CHART_BYTES = 10 * 1024 * 1024


@dataclass
class SandboxResult:
    ok: bool
    chart_path: str | None = None
    error: str | None = None
    logs: str = ""


class BaseSandbox:
    name = "base"

    def run(self, code: str, data: dict, out_dir: str | Path) -> SandboxResult:
        raise NotImplementedError

    def _prepare(self, code: str, data: dict) -> str:
        workdir = tempfile.mkdtemp(prefix="deepquery-chart-")
        Path(workdir, "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        Path(workdir, "chart.py").write_text(code, encoding="utf-8")
        return workdir

    def _collect(self, workdir: str, out_dir: str | Path, logs: str) -> SandboxResult:
        """回收产物。chart.png 由不受信代码写出，回收时同样不能信任：

        不跟随符号链接（否则 os.symlink('/app/.env', 'chart.png') 就能让宿主机
        把任意文件当作"图表"对外提供）、只接受链接数为 1 的普通文件、校验 PNG
        签名与大小，最后按字节拷贝到输出目录而不是 move（move 会把链接本身搬过去）。
        """
        chart = Path(workdir) / "chart.png"
        missing = SandboxResult(ok=False, error="代码执行完成但没有生成 chart.png", logs=logs)
        rejected = SandboxResult(
            ok=False, error="chart.png 不是普通文件（疑似符号链接/硬链接），已拒绝", logs=logs
        )
        if chart.is_symlink():  # 无 O_NOFOLLOW 的平台也能挡住
            return rejected
        try:
            fd = os.open(chart, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return missing
        except OSError:  # ELOOP：打开时才被换成了链接
            return rejected
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                return rejected
            payload = fh.read(_MAX_CHART_BYTES + 1)
        if not payload:
            return missing
        if len(payload) > _MAX_CHART_BYTES:
            return SandboxResult(ok=False, error="chart.png 超过 10MB 上限，已拒绝", logs=logs)
        if not payload.startswith(_PNG_MAGIC):
            return SandboxResult(ok=False, error="chart.png 不是合法的 PNG 文件，已拒绝", logs=logs)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"chart-{uuid.uuid4().hex[:12]}.png"
        target.write_bytes(payload)
        return SandboxResult(ok=True, chart_path=str(target), logs=logs)


class SubprocessSandbox(BaseSandbox):
    name = "subprocess"

    def __init__(self, timeout_seconds: float = 20, memory_mb: int = 512):
        self.timeout_seconds = timeout_seconds
        self.memory_mb = memory_mb

    def run(self, code: str, data: dict, out_dir: str | Path) -> SandboxResult:
        workdir = self._prepare(code, data)
        try:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "MPLBACKEND": "Agg",  # 无显示环境
                "HOME": workdir,
            }

            def limits():  # 子进程资源限额（POSIX，逐项 best-effort）
                import resource

                mem = self.memory_mb * 1024 * 1024
                cpu = max(1, int(self.timeout_seconds))
                for res, lim in (
                    (resource.RLIMIT_AS, (mem, mem)),
                    (resource.RLIMIT_CPU, (cpu, cpu)),
                    (resource.RLIMIT_FSIZE, (20 * 1024 * 1024, 20 * 1024 * 1024)),
                ):
                    try:
                        resource.setrlimit(res, lim)
                    except (ValueError, OSError):
                        # macOS 等平台不支持部分限额（如 RLIMIT_AS 会 EINVAL）。
                        # 跳过该项：墙钟 timeout 仍是硬保证，生产隔离靠 Docker。
                        pass

            proc = subprocess.run(
                [sys.executable, "-I", "chart.py"],
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                preexec_fn=limits if os.name == "posix" else None,
            )
            logs = (proc.stdout + "\n" + proc.stderr).strip()
            if proc.returncode in (-9, -24):  # SIGKILL/SIGXCPU：CPU 限额先于墙钟超时触发
                return SandboxResult(
                    ok=False, error=f"执行超时（CPU 限额 {int(self.timeout_seconds)}s）", logs=logs[-2000:]
                )
            if proc.returncode != 0:
                return SandboxResult(ok=False, error=f"退出码 {proc.returncode}", logs=logs[-2000:])
            return self._collect(workdir, out_dir, logs[-2000:])
        except subprocess.TimeoutExpired:
            return SandboxResult(ok=False, error=f"执行超时（>{self.timeout_seconds}s）")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


class DockerSandbox(BaseSandbox):
    name = "docker"

    def __init__(self, image: str, timeout_seconds: float = 20, memory_mb: int = 512):
        self.image = image
        self.timeout_seconds = timeout_seconds
        self.memory_mb = memory_mb

    def run(self, code: str, data: dict, out_dir: str | Path) -> SandboxResult:
        workdir = self._prepare(code, data)
        try:
            proc = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "--network", "none",
                    "--memory", f"{self.memory_mb}m",
                    "--cpus", "1",
                    "--pids-limit", "64",
                    "-v", f"{workdir}:/work",
                    "-w", "/work",
                    self.image,
                    "python", "chart.py",
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds + 15,  # 容器启动余量
            )
            logs = (proc.stdout + "\n" + proc.stderr).strip()
            if proc.returncode != 0:
                return SandboxResult(ok=False, error=f"退出码 {proc.returncode}", logs=logs[-2000:])
            return self._collect(workdir, out_dir, logs[-2000:])
        except subprocess.TimeoutExpired:
            return SandboxResult(ok=False, error=f"执行超时（>{self.timeout_seconds}s）")
        except FileNotFoundError:
            return SandboxResult(ok=False, error="docker 命令不可用")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def docker_available() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def build_sandbox(settings: "Settings") -> BaseSandbox:
    mode = settings.chart_executor
    if mode == "docker" or (mode == "auto" and docker_available()):
        return DockerSandbox(settings.chart_image, settings.chart_timeout_seconds)
    return SubprocessSandbox(settings.chart_timeout_seconds)
