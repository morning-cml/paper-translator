"""配置加载。

优先级（从高到低）：
    1. 传入的显式参数
    2. 项目根目录的 config.json
    3. 环境变量 DEEPSEEK_API_KEY / DEEPSEEK_MODEL / DEEPSEEK_BASE_URL
    4. 内置默认值
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from .paths import data_file, resource_dir, user_path

# 只读资源根（源码运行=程序/；打包运行=解包目录）
ROOT = resource_dir()
# config.json 必须写在**可写且跨版本保留**的位置，打包后不能落在临时解包目录
CONFIG_PATH = user_path("config.json")

DEFAULTS = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-pro",
    "temperature": 1.0,
    # B5 多语言：源/目标用语言代码（auto/en/zh/ja/ko/de/fr/es/ru/pt/it）
    "source_lang": "auto",
    "target_lang": "zh",
    # translated = 纯译文（覆盖原文）；bilingual = 双语对照（原文页 + 译文页）
    "output_mode": "translated",
    "glossary_path": "glossary/cs_terms.csv",
    "batch_size": 8,       # 每次 API 调用翻译的段落数
    "max_workers": 8,      # 并发请求数（越大越快，占用更多 token/连接）
    "thinking": True,      # 默认开启=质量优先（更准）；设 False 可明显提速
    "use_system_proxy": True,  # False=忽略系统/VPN代理，直连
    "proxy": "",               # 如 "http://127.0.0.1:7890"，留空表示不指定
    "verify_ssl": True,        # False=跳过证书校验（安全软件MITM时用，谨慎）
    # 回填后端：auto=优先 PyMuPDF、缺则回退 reportlab；可强制 pymupdf / reportlab
    "render_backend": "auto",
    # 中文字体：留空则自动找 fonts/ 目录（思源/Noto 等 ttf/otf），找不到用内置
    "font_path": "",
    # 持久化翻译缓存（cache/translations.json）：重跑同一文档不重复计费
    "use_cache": True,
    # 强制重译：无视已有缓存重新翻译并覆盖旧结果（上次翻得不对时用；不落盘持久化）
    "refresh_cache": False,
    # 学科领域：影响翻译提示词口径（计算机科学/通用学术/生物医学/物理学…）
    "domain": "计算机科学",
    # 本文术语表：译前自动抽取全文高频术语、一次性译出并全篇统一（对齐 BabelDOC）。
    # 只多花一次请求且进缓存；关掉则完全沿用静态 CSV 术语库。
    "auto_glossary": True,
    "auto_glossary_limit": 30,     # 最多抽多少条
    # 非空则把自动抽取的术语表导出成同格式 CSV，便于人工挑拣后并进静态库
    "save_glossary_path": "",
    # 试译页数：0=翻译整篇；N=只翻译前 N 页（其余页保留原文，便宜预览）
    "max_pages": 0,
    # 页码范围（1 起）："1-3,5,8-"。非空则**优先于 max_pages**；用于只翻指定
    # 章节，或把大文档分批翻（先 1-50 再 51-100，已译段走缓存不重复计费）
    "page_range": "",
    # 公式检测的**追加**规则（正则，只加不减，内置表照旧）：
    # font=字体名里出现即当数学字体；char=该字符必须整块图像保护、不参与重排。
    # 排版千奇百怪，内置表收不全时不必改源码。留空即完全沿用内置行为。
    "formula_font_pattern": "",
    "formula_char_pattern": "",
    # 计费单价（元/百万 token，用于翻译前成本预估显示；0=只显示 token 数）
    "price_in": 0.0,
    "price_out": 0.0,
}


@dataclass
class Config:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-pro"
    temperature: float = 1.0
    source_lang: str = "auto"
    target_lang: str = "zh"
    output_mode: str = "translated"
    glossary_path: str = "glossary/cs_terms.csv"
    batch_size: int = 8
    max_workers: int = 8
    thinking: bool = True
    use_system_proxy: bool = True
    proxy: str = ""
    verify_ssl: bool = True
    render_backend: str = "auto"
    font_path: str = ""
    use_cache: bool = True
    refresh_cache: bool = False
    domain: str = "计算机科学"
    auto_glossary: bool = True
    auto_glossary_limit: int = 30
    save_glossary_path: str = ""
    max_pages: int = 0
    page_range: str = ""
    formula_font_pattern: str = ""
    formula_char_pattern: str = ""
    price_in: float = 0.0
    price_out: float = 0.0

    def resolved_glossary_path(self) -> Path:
        p = Path(self.glossary_path)
        if p.is_absolute():
            return p
        # 相对路径：优先用用户自己编辑过的副本，否则用随程序分发的默认术语库
        return data_file(*p.parts)

    def to_dict(self) -> dict:
        return asdict(self)


def _from_file() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[config] 读取 config.json 失败，忽略：{e}")
    return {}


def _from_env() -> dict:
    out = {}
    if os.environ.get("DEEPSEEK_API_KEY"):
        out["api_key"] = os.environ["DEEPSEEK_API_KEY"]
    if os.environ.get("DEEPSEEK_MODEL"):
        out["model"] = os.environ["DEEPSEEK_MODEL"]
    if os.environ.get("DEEPSEEK_BASE_URL"):
        out["base_url"] = os.environ["DEEPSEEK_BASE_URL"]
    return out


def load_config(**overrides) -> Config:
    """按优先级合并配置。overrides 中值为 None 的键会被忽略。"""
    merged = dict(DEFAULTS)
    merged.update(_from_file())
    merged.update(_from_env())
    merged.update({k: v for k, v in overrides.items() if v is not None})
    _migrate_langs(merged)   # 旧 config.json 的中文语言名 → 语言代码
    # 仅保留 Config 已知字段
    known = {f for f in Config().__dict__}
    merged = {k: v for k, v in merged.items() if k in known}
    return Config(**merged)


# 旧版本用中文名存语言（"英文"/"中文"），迁移到 B5 的语言代码
_LEGACY_LANG = {"英文": "en", "英语": "en", "中文": "zh", "简体中文": "zh",
                "自动": "auto", "自动检测": "auto", "日文": "ja", "日语": "ja",
                "德文": "de", "德语": "de", "法文": "fr", "法语": "fr"}


def _migrate_langs(merged: dict) -> None:
    for key in ("source_lang", "target_lang"):
        v = merged.get(key)
        if isinstance(v, str) and v in _LEGACY_LANG:
            merged[key] = _LEGACY_LANG[v]


def save_config(cfg: Config) -> None:
    """把当前配置写入 config.json（含 API Key，请勿提交到版本库）。"""
    data = json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)  # 原子替换，避免写一半导致文件损坏
