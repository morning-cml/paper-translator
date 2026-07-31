"""pytest 公共夹具。

运行：在 程序/ 目录下 `py -m pytest tests -q`
（首次需 `py -m pip install pytest`）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def _isolate_user_config(tmp_path_factory):
    """让所有用例都从**纯默认配置**起步，不读开发机上的真实 config.json / 环境变量。

    源码模式下 `load_config()` 会读 `程序/config.json`——你自己用软件时存下的
    设置（如 output_mode=sidebyside、batch_size=12）会渗进断言，导致"CI 干净所以
    全绿、本机却红"的假象。把 CONFIG_PATH 指到空临时目录、清掉相关环境变量即可
    彻底隔离。需要特定配置的用例仍可显式 `load_config(output_mode=...)`。

    必须是 **session 级**：docx/pptx 等用例的 `translated` 夹具是 module 级、
    在建立时就调用 `load_config()`；只有比它更早建立的 session 夹具才拦得住。
    """
    import src.config as _config
    mp = pytest.MonkeyPatch()
    fake = tmp_path_factory.mktemp("home") / "config.json"
    mp.setattr(_config, "CONFIG_PATH", fake)
    for var in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL"):
        mp.delenv(var, raising=False)
    yield
    mp.undo()


# 真实论文的结构性回归基准。**不入库**（论文有版权），所以只能靠约定定位。
#
# ⚠️ 这里曾经是个隐患：路径硬编码成一个文件名，文件一旦被挪走，11 项回归就
# 整批静默跳过——其中包括「可译块数 120~145」这条，正是用来卡解析内核改动的。
# 2026-07-31 真发生过：论文被移到别的目录，测试从 248 passed 变成 237 passed +
# 11 skipped，而 `-q` 的输出里 skipped 毫不起眼，差点带着未经回归的解析改动提交。
#
# 现在：可用 PAPER_TRANSLATOR_TEST_PAPER 指定任意路径；本机缺失时在结果末尾
# **显式告警**（见 pytest_terminal_summary），不再无声无息。CI 上本就没有这份
# 论文，属预期跳过，不告警。
_PAPER_NAME = ("Observing a robot peer's failures facilitates "
               "students' classroom learning.pdf")
_PAPER_ENV = "PAPER_TRANSLATOR_TEST_PAPER"
SAMPLE = ROOT / "samples" / "sample_paper.pdf"


def _find_paper() -> Path | None:
    override = os.environ.get(_PAPER_ENV)
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None
    p = ROOT.parent / _PAPER_NAME          # 约定位置：程序/ 的上一级
    return p if p.is_file() else None


PAPER = _find_paper()
_IN_CI = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """基准论文缺失时把话说明白——静默跳过比测试失败更危险。"""
    if PAPER is not None or _IN_CI:
        return
    n = len(terminalreporter.stats.get("skipped", []))
    terminalreporter.write_sep("=", "基准论文缺失", yellow=True, bold=True)
    terminalreporter.write_line(
        f"未找到《{_PAPER_NAME}》，{n} 项真实论文回归**未执行**"
        f"（含解析结构不变量）。")
    terminalreporter.write_line(
        f"放回 {ROOT.parent} 即可，或设 {_PAPER_ENV}=<pdf 路径> 指向别处。")


@pytest.fixture(scope="session")
def paper_path():
    if PAPER is None:
        pytest.skip(f"基准论文不在工作区（可用 {_PAPER_ENV} 指定路径）")
    return str(PAPER)


@pytest.fixture(scope="session")
def sample_path():
    if not SAMPLE.exists():
        pytest.skip("samples/sample_paper.pdf 缺失")
    return str(SAMPLE)


@pytest.fixture(scope="session")
def paper_layouts(paper_path):
    """真实论文的解析结果（整份解析较慢，全会话复用）。"""
    from src.pdf_parser import parse_pdf
    return parse_pdf(paper_path)
