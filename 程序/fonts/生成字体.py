"""从 Noto CJK **可变字体**实例化出本工具要用的两个静态字重。

为什么需要这一步：PyMuPDF 加载可变字体时取的是**默认实例**，而 Noto CJK 的
默认实例在字重轴的最细端（Sans=Thin/100、Serif=ExtraLight/200）。直接把 VF
丢给它，正文会变成极细体，比用内置字体还糟。必须先烘焙成静态字重。

产物（本目录下）：
    NotoSerifSC-Regular.ttf   正文（宋体，wght=400）
    NotoSansSC-Bold.ttf       标题（黑体加粗，wght=700）

中文排版惯例是「正文宋体、标题黑体」；用真字重取代原先的描边合成加粗——
汉字笔画本就密，描粗只会糊成一团，真黑体是重新设计的笔形。

字体来源：Noto Sans SC / Noto Serif SC，**SIL Open Font License 1.1**，
可随本项目（AGPL-3.0）自由分发。系统里没有时，从
https://github.com/notofonts/noto-cjk/releases 下载可变字体版本即可。

用法：  py fonts/生成字体.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# (输出名, 可变字体候选路径, 目标字重)
TARGETS = [
    ("NotoSerifSC-Regular.ttf", "NotoSerifSC-VF.ttf", 400),
    ("NotoSansSC-Bold.ttf", "NotoSansSC-VF.ttf", 700),
]

SEARCH_DIRS = [
    Path(r"C:\Windows\Fonts"),
    Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
    Path.home() / "Library/Fonts",
    Path("/usr/share/fonts/opentype/noto"),
    Path("/usr/share/fonts/truetype/noto"),
    HERE,                      # 也允许把 VF 直接放本目录
]


def _find(name: str) -> Path | None:
    for d in SEARCH_DIRS:
        p = d / name
        if p.is_file():
            return p
    return None


def main() -> int:
    try:
        from fontTools import ttLib
        from fontTools.varLib import instancer
    except ImportError:
        print("缺少 fonttools，请先： py -m pip install fonttools", file=sys.stderr)
        return 1

    made = 0
    for out_name, vf_name, weight in TARGETS:
        out = HERE / out_name
        if out.is_file():
            print(f"  已存在，跳过：{out_name}")
            continue
        src = _find(vf_name)
        if src is None:
            print(f"  [!] 找不到 {vf_name}，跳过 {out_name}。"
                  f"可从 https://github.com/notofonts/noto-cjk/releases 下载后"
                  f"放进 {HERE}", file=sys.stderr)
            continue
        print(f"  {vf_name} @wght={weight} → {out_name} …", flush=True)
        font = ttLib.TTFont(str(src))
        # updateFontNames=True 必须开：否则 name 表沿用可变字体的默认实例名，
        # 烘焙出来的 Bold 会自称 "Noto Sans SC Thin"（轮廓是对的、名字是错的）。
        # 排查问题时看到这个名字必然被带偏，且 fitz 的 is_bold 会误判为否。
        static = instancer.instantiateVariableFont(
            font, {"wght": weight}, updateFontNames=True)
        static.save(str(out))
        made += 1
        print(f"      {out.stat().st_size / 1048576:.1f} MB")

    print(f"\n完成，新生成 {made} 个。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
