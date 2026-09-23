"""图表代码沙箱：执行模型生成的 Python 画图代码。

模型生成的代码是不受信内容，绝不允许在主进程里 exec。两种执行器：
- DockerSandbox（生产首选）：--network none 断网 + 内存/CPU 限额 + 只读挂载工作目录，
  镜像见 docker/chart-sandbox/Dockerfile；
- SubprocessSandbox（开发兜底 / 容器内运行时）：独立子进程 + resource 限额
  （地址空间/CPU 时间/文件大小）+ 隔离模式 python -I + 只保留必要的环境变量。
  服务以 root 运行时（容器内），每次执行换成一个独立的无权限 uid：
  读不到 root 进程的 /proc/*/environ（里面有 LLM_API_KEY），读不到 0700 的数据目录，
  进程数有上限（挡 fork 炸弹），结束后按 uid 清掉它留下的所有进程。
  它仍不隔离网络，安全性弱于 Docker——适合自身已跑在容器里的场景。

代码契约（写进提示词）：工作目录有 data.json（{"columns": [...], "rows": [...]})，
代码读取它并把图保存为 chart.png；只允许用 matplotlib/标准库。
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


# 完整 8 字节 PNG 签名；产物上限 10MB（正常图表几十到几百 KB）
# 统一图表样式：写进工作目录的 matplotlibrc（matplotlib 优先读取当前目录下的这个文件），
# 模型写的画图代码不用管配色和字体，也就不会各画各的风格。
# 颜色取自界面主色 + 经色盲安全校验的分类色板；rc 文件里 # 是注释，所以颜色不带 #
MATPLOTLIBRC = """\
figure.figsize: 8, 4.6
figure.dpi: 100
figure.facecolor: fbf8f3
savefig.dpi: 200
savefig.facecolor: fbf8f3
savefig.bbox: tight
savefig.pad_inches: 0.3
font.family: sans-serif
font.sans-serif: WenQuanYi Micro Hei, WenQuanYi Zen Hei, Noto Sans CJK SC, Source Han Sans SC, PingFang SC, Microsoft YaHei, DejaVu Sans
font.size: 10.5
axes.unicode_minus: False
axes.facecolor: fbf8f3
axes.edgecolor: d9d0c3
axes.linewidth: 0.8
axes.spines.top: False
axes.spines.right: False
axes.spines.left: False
axes.grid: True
axes.grid.axis: y
axes.axisbelow: True
axes.formatter.useoffset: False
axes.formatter.limits: -7, 12
axes.titlesize: 13
axes.titleweight: bold
axes.titlelocation: left
axes.titlepad: 14
axes.titlecolor: 201e1d
axes.labelsize: 10
axes.labelcolor: 645c50
axes.labelpad: 8
axes.prop_cycle: cycler('color', ['c67139', '2a78d6', '1baf7a', '4a3aa7', 'e87ba4', '008300', 'eda100', 'e34948'])
grid.color: e9e2d6
grid.linewidth: 0.8
xtick.color: 82796a
ytick.color: 82796a
xtick.labelcolor: 645c50
ytick.labelcolor: 201e1d
xtick.major.size: 0
ytick.major.size: 0
xtick.major.pad: 6
ytick.major.pad: 8
text.color: 201e1d
legend.frameon: False
legend.fontsize: 9.5
lines.linewidth: 2.2
lines.markersize: 6
lines.solid_capstyle: round
patch.linewidth: 0
"""

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MAX_CHART_BYTES = 10 * 1024 * 1024
_KEEP_CHARTS = 500  # 输出目录只保留最近的图表，防止磁盘被慢慢写满

# 以 root 运行时，每次执行分配一个独立 uid（不需要在 /etc/passwd 里存在）：
# 并发的多次执行互不可见，结束后可以按 uid 精确清理
_SANDBOX_UID_BASE = 61000
_SANDBOX_UID_SPAN = 1000
_uid_counter = itertools.count()
_uid_lock = threading.Lock()


def _next_sandbox_uid() -> int:
    with _uid_lock:
        return _SANDBOX_UID_BASE + next(_uid_counter) % _SANDBOX_UID_SPAN


def _drop_to(uid: int) -> None:
    os.setgroups([])
    os.setgid(uid)
    os.setuid(uid)


def _kill_uid(uid: int) -> None:
    """杀掉该 uid 的全部进程（含脱离进程组的孙进程）：以该 uid 身份执行 kill -9 -1。"""
    try:
        subprocess.run(["sh", "-c", "kill -9 -1"], preexec_fn=lambda: _drop_to(uid), timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _prune(out_dir: Path, keep: int = _KEEP_CHARTS) -> None:
    charts = sorted(out_dir.glob("chart-*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in charts[keep:]:
        old.unlink(missing_ok=True)


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
        Path(workdir, "matplotlibrc").write_text(MATPLOTLIBRC, encoding="utf-8")
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
        _prune(out_dir)
        return SandboxResult(ok=True, chart_path=str(target), logs=logs)


class SubprocessSandbox(BaseSandbox):
    name = "subprocess"

    def __init__(self, timeout_seconds: float = 20, memory_mb: int = 512):
        self.timeout_seconds = timeout_seconds
        self.memory_mb = memory_mb

    def run(self, code: str, data: dict, out_dir: str | Path) -> SandboxResult:
        workdir = self._prepare(code, data)
        # 只有 root 才能切换身份；本机开发（非 root）时退化为同用户子进程
        uid = _next_sandbox_uid() if os.name == "posix" and os.geteuid() == 0 else None
        proc: subprocess.Popen | None = None
        try:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "MPLBACKEND": "Agg",  # 无显示环境
                "HOME": workdir,
                "OMP_NUM_THREADS": "1",  # 数值库别按 CPU 数开线程（线程也计入进程数上限）
                "OPENBLAS_NUM_THREADS": "1",
            }
            if os.environ.get("MPLCONFIGDIR"):  # 镜像里预建的字体缓存，省掉每次几秒的字体扫描
                env["MPLCONFIGDIR"] = os.environ["MPLCONFIGDIR"]
            if uid is not None:
                os.chown(workdir, uid, uid)  # 子进程要在工作目录里写 chart.png

            def limits():  # 子进程资源限额（POSIX，逐项 best-effort）
                import resource

                mem = self.memory_mb * 1024 * 1024
                cpu = max(1, int(self.timeout_seconds))
                caps = [
                    (resource.RLIMIT_AS, (mem, mem)),
                    (resource.RLIMIT_CPU, (cpu, cpu)),
                    (resource.RLIMIT_FSIZE, (20 * 1024 * 1024, 20 * 1024 * 1024)),
                ]
                if uid is not None:
                    # 进程数上限按 uid 计：只有换成独立 uid 后才能设，否则会连带限制服务自身
                    caps.append((resource.RLIMIT_NPROC, (32, 32)))
                for res, lim in caps:
                    try:
                        resource.setrlimit(res, lim)
                    except (ValueError, OSError):
                        # macOS 等平台不支持部分限额（如 RLIMIT_AS 会 EINVAL）。
                        # 跳过该项：墙钟 timeout 仍是硬保证，生产隔离靠 Docker。
                        pass
                if uid is not None:
                    _drop_to(uid)

            # 输出写到主进程持有的临时文件而不是管道：代码 fork 出的后台子进程会继承管道，
            # 用管道就得等它们全部退出才能读完，一个留后台的进程就能让每次执行都拖到超时
            with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as out:
                try:
                    proc = subprocess.Popen(
                        [sys.executable, "-I", "chart.py"],
                        cwd=workdir,
                        env=env,
                        stdout=out,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,  # 独立进程组：超时时连同它的子进程一起杀掉
                        preexec_fn=limits if os.name == "posix" else None,
                    )
                except (OSError, subprocess.SubprocessError) as e:
                    # 典型原因：Python 解释器装在隔离用户读不到的目录里。图表失败不能拖垮整次提问
                    return SandboxResult(ok=False, error=f"图表沙箱启动失败：{e}")
                try:
                    returncode = proc.wait(timeout=self.timeout_seconds)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    return SandboxResult(ok=False, error=f"执行超时（>{self.timeout_seconds}s）")
                out.seek(0)
                logs = out.read(200_000).strip()
            if returncode in (-9, -24):  # SIGKILL/SIGXCPU：CPU 限额先于墙钟超时触发
                return SandboxResult(
                    ok=False, error=f"执行超时（CPU 限额 {int(self.timeout_seconds)}s）", logs=logs[-2000:]
                )
            if returncode != 0:
                return SandboxResult(ok=False, error=f"退出码 {returncode}", logs=logs[-2000:])
            return self._collect(workdir, out_dir, logs[-2000:])
        finally:
            if uid is not None:
                _kill_uid(uid)  # 清掉脱离进程组、还在后台的孙进程
            elif proc is not None and os.name == "posix":
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
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
