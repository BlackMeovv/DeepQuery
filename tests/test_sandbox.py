"""沙箱执行器测试（SubprocessSandbox，离线）。"""

import os
from pathlib import Path

import pytest

from deepquery.agent import DeepQuery
from deepquery.agent.graph import extract_code
from deepquery.llm import MockLLM
from deepquery.sandbox import _SANDBOX_UID_BASE, _SANDBOX_UID_SPAN, SubprocessSandbox, _prune

as_root = pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0, reason="换 uid 隔离只在以 root 运行时启用")


def _sandbox_processes() -> list[int]:
    """当前还活着的、属于沙箱 uid 段的进程（已被杀、只等回收的僵尸进程不算）。"""
    alive = []
    for status in Path("/proc").glob("[0-9]*/status"):
        try:
            fields = dict(line.split(":", 1) for line in status.read_text().splitlines() if ":" in line)
        except OSError:
            continue
        uid = int(fields.get("Uid", "-1").split()[0])
        zombie = fields.get("State", "").strip().startswith("Z")
        if _SANDBOX_UID_BASE <= uid < _SANDBOX_UID_BASE + _SANDBOX_UID_SPAN and not zombie:
            alive.append(int(status.parent.name))
    return alive

# 不依赖 matplotlib 的"画图"代码：直接写一个 PNG 头，验证执行器机制本身
FAKE_CHART_CODE = """
import json
data = json.load(open("data.json"))
assert data["columns"] and data["rows"]
open("chart.png", "wb").write(b"\\x89PNG\\r\\n\\x1a\\n" + str(len(data["rows"])).encode())
"""


class TestSubprocessSandbox:
    def test_success(self, tmp_path):
        sandbox = SubprocessSandbox(timeout_seconds=10)
        result = sandbox.run(FAKE_CHART_CODE, {"columns": ["a"], "rows": [[1], [2]]}, tmp_path)
        assert result.ok and result.chart_path
        assert open(result.chart_path, "rb").read().startswith(b"\x89PNG")

    def test_no_output_file(self, tmp_path):
        result = SubprocessSandbox(timeout_seconds=10).run(
            "print('did nothing')", {"columns": [], "rows": []}, tmp_path
        )
        assert not result.ok and "chart.png" in result.error

    def test_crash_reports_logs(self, tmp_path):
        result = SubprocessSandbox(timeout_seconds=10).run(
            "raise RuntimeError('boom')", {"columns": [], "rows": []}, tmp_path
        )
        assert not result.ok and "boom" in result.logs

    def test_symlink_artifact_rejected(self, tmp_path):
        # 回归：不受信代码把 chart.png 做成指向敏感文件的符号链接，宿主机不得跟随
        secret = tmp_path / "secret.env"
        secret.write_text("LLM_API_KEY=sk-should-never-leak")
        code = f"import os\nos.symlink({str(secret)!r}, 'chart.png')"
        out = tmp_path / "charts"
        result = SubprocessSandbox(timeout_seconds=10).run(code, {"columns": [], "rows": []}, out)
        assert not result.ok and "符号链接" in (result.error or "")
        assert not out.exists() or not any(out.iterdir())  # 输出目录里什么都没落下

    def test_hardlink_artifact_rejected(self, tmp_path):
        secret = tmp_path / "secret.env"
        secret.write_bytes(b"\x89PNG\r\n\x1a\n" + b"LLM_API_KEY=sk-leak")  # 连魔数都伪造了
        code = f"import os\nos.link({str(secret)!r}, 'chart.png')"
        out = tmp_path / "o"
        result = SubprocessSandbox(timeout_seconds=10).run(code, {"columns": [], "rows": []}, out)
        # 非 root 运行：链接建成后在回收时被拒；root 运行：子进程换了 uid，连链接都建不了
        assert not result.ok
        assert "硬链接" in (result.error or "") or "Permission denied" in result.logs
        assert not out.exists() or not any(out.iterdir())

    def test_non_png_rejected(self, tmp_path):
        code = "open('chart.png', 'w').write('LLM_API_KEY=sk-leak')"
        result = SubprocessSandbox(timeout_seconds=10).run(code, {"columns": [], "rows": []}, tmp_path)
        assert not result.ok and "PNG" in (result.error or "")

    def test_timeout(self, tmp_path):
        result = SubprocessSandbox(timeout_seconds=1).run(
            "while True: pass", {"columns": [], "rows": []}, tmp_path
        )
        assert not result.ok and "超时" in (result.error or "")


class TestPrivilegeDrop:
    """容器内以 root 运行时的隔离：换 uid、进程数上限、结束后清理。"""

    @as_root
    def test_cannot_read_parent_environment(self, tmp_path):
        code = "import os\nopen(f'/proc/{os.getppid()}/environ').read()"
        result = SubprocessSandbox(timeout_seconds=10).run(code, {"columns": [], "rows": []}, tmp_path)
        assert not result.ok and "Permission denied" in result.logs

    @as_root
    def test_process_count_is_capped_and_leftovers_are_killed(self, tmp_path):
        code = (
            "import os, time\n"
            "n = 0\n"
            "try:\n"
            "    for _ in range(100):\n"
            "        if os.fork() == 0:\n"
            "            time.sleep(60)\n"
            "            os._exit(0)\n"
            "        n += 1\n"
            "except OSError:\n"
            "    pass\n"
            "open('chart.png', 'wb').write(b'\\x89PNG\\r\\n\\x1a\\n' + str(n).encode())\n"
        )
        result = SubprocessSandbox(timeout_seconds=10).run(code, {"columns": [], "rows": []}, tmp_path)
        assert result.ok
        forked = int(open(result.chart_path, "rb").read()[8:])
        assert forked < 100  # 进程数上限生效
        assert _sandbox_processes() == []  # 后台睡着的子进程都被清理了


def test_old_charts_are_pruned(tmp_path):
    for i in range(5):
        p = tmp_path / f"chart-{i:012d}.png"
        p.write_bytes(b"x")
        os.utime(p, (i, i))
    _prune(tmp_path, keep=3)
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"chart-{i:012d}.png" for i in (2, 3, 4)]


class TestChartNode:
    def sql_reply(self, sql):
        return f"思路。\n```sql\n{sql}\n```"

    def test_chart_generated_via_graph(self, settings, db, tmp_path):
        cfg = settings.model_copy(
            update={"chart_executor": "subprocess", "chart_out_dir": str(tmp_path)}
        )
        agent = DeepQuery(
            cfg,
            db,
            MockLLM(
                [
                    self.sql_reply("SELECT status, COUNT(*) FROM orders GROUP BY status"),
                    f"图型选择说明。\n```python\n{FAKE_CHART_CODE}\n```",
                ]
            ),
        )
        outcome = agent.ask("各状态订单分布", generate_answer=False, generate_chart=True)
        assert outcome.status == "ok"
        assert outcome.chart_path and outcome.chart_error is None

    def test_denylist_blocks_dangerous_code(self, settings, db, tmp_path):
        cfg = settings.model_copy(
            update={"chart_executor": "subprocess", "chart_out_dir": str(tmp_path)}
        )
        agent = DeepQuery(
            cfg,
            db,
            MockLLM(
                [
                    self.sql_reply("SELECT COUNT(*) FROM customers"),
                    "```python\nimport subprocess\nsubprocess.run(['curl', 'evil'])\n```",
                ]
            ),
        )
        outcome = agent.ask("客户数", generate_answer=False, generate_chart=True)
        assert outcome.status == "ok"  # 查询本身成功
        assert outcome.chart_path is None
        assert "禁止" in outcome.chart_error

    def test_chart_failure_does_not_break_run(self, settings, db, tmp_path):
        cfg = settings.model_copy(
            update={"chart_executor": "subprocess", "chart_out_dir": str(tmp_path)}
        )
        agent = DeepQuery(
            cfg,
            db,
            MockLLM(
                [
                    self.sql_reply("SELECT COUNT(*) FROM customers"),
                    "```python\nraise RuntimeError('bad chart')\n```",
                ]
            ),
        )
        outcome = agent.ask("客户数", generate_answer=False, generate_chart=True)
        assert outcome.status == "ok" and outcome.chart_error


class TestExtractCode:
    def test_prefers_python_tag(self):
        text = "```json\n{}\n```\n```python\nprint(1)\n```"
        assert extract_code(text) == "print(1)"

    def test_plain_fence_fallback(self):
        assert extract_code("```\nprint(2)\n```") == "print(2)"
