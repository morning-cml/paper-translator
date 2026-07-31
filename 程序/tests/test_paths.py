"""原子写出与临时残留清理（防"中途失败留下半截损坏产物 / 垃圾文件"），
以及程序改名后的用户数据目录迁移（防"老用户 Key 与付费缓存失联"）。"""
from pathlib import Path

import pytest

from src import paths
from src.paths import OutputError, _adopt_legacy_dir, atomic_output, sweep_temp


def test_atomic_output_success_replaces_and_leaves_no_part(tmp_path):
    final = tmp_path / "out.txt"
    with atomic_output(str(final)) as h:
        assert h.tmp.endswith(".part")
        Path(h.tmp).write_text("done", encoding="utf-8")
        assert not final.exists(), "落位前最终文件不该出现"
    assert final.read_text(encoding="utf-8") == "done"
    assert h.path == str(final)
    assert not Path(h.tmp).exists(), "成功后不留 .part"


def test_atomic_output_failure_leaves_nothing(tmp_path):
    final = tmp_path / "out.txt"
    with pytest.raises(RuntimeError, match="boom"):
        with atomic_output(str(final)) as h:
            Path(h.tmp).write_text("partial", encoding="utf-8")
            raise RuntimeError("boom")
    assert not final.exists(), "失败绝不能在最终路径留下半截文件"
    assert not (tmp_path / "out.txt.part").exists(), "失败要清掉 .part"


def test_atomic_output_does_not_clobber_existing_on_failure(tmp_path):
    """已有一份好文件时，新一次生成失败不得破坏它。"""
    final = tmp_path / "out.txt"
    final.write_text("GOOD", encoding="utf-8")
    with pytest.raises(RuntimeError):
        with atomic_output(str(final)) as h:
            Path(h.tmp).write_text("half", encoding="utf-8")
            raise RuntimeError("x")
    assert final.read_text(encoding="utf-8") == "GOOD", "旧的好文件应原样保留"


def test_atomic_output_locked_target_falls_back_to_free_name(tmp_path):
    """最终名无法覆盖（这里用"已是目录"模拟被占用）→ 换个名保住成果。"""
    final = tmp_path / "out.pdf"
    final.mkdir()                       # 占住最终名，os.replace 会失败
    with atomic_output(str(final)) as h:
        Path(h.tmp).write_text("data", encoding="utf-8")
    assert h.path != str(final), "应改用不冲突的名字"
    assert Path(h.path).read_text(encoding="utf-8") == "data"
    assert not Path(h.tmp).exists()


def test_sweep_temp_removes_only_our_siblings(tmp_path):
    base = tmp_path / "translations.json"
    base.write_text("{}", encoding="utf-8")
    (tmp_path / "translations.json.tmp").write_text("x", encoding="utf-8")
    (tmp_path / "translations.json.part").write_text("y", encoding="utf-8")
    keep = tmp_path / "user.txt"
    keep.write_text("keep", encoding="utf-8")

    sweep_temp(str(base))
    assert not (tmp_path / "translations.json.tmp").exists()
    assert not (tmp_path / "translations.json.part").exists()
    assert base.exists() and keep.exists(), "只删自己的 .tmp/.part，别的不动"


# --------------------------------------------------------------------------
# 改名迁移：旧名目录里是老用户的 API Key 和花过钱的翻译缓存，一条都不能丢
# --------------------------------------------------------------------------

def _seed_legacy(base: Path) -> Path:
    """造一个 v1.1.0 时代的用户数据目录。"""
    old = base / paths._LEGACY_APP_NAMES[0]
    (old / "cache").mkdir(parents=True)
    (old / "config.json").write_text('{"api_key":"sk-old"}', encoding="utf-8")
    (old / "cache" / "translations.json").write_text('{"hit":1}', encoding="utf-8")
    return old


def test_legacy_dir_is_adopted_with_key_and_cache_intact(tmp_path):
    """老用户升级：旧名目录整体改名过来，Key 和缓存原样还在。"""
    old = _seed_legacy(tmp_path)

    got = _adopt_legacy_dir(tmp_path)

    assert got == tmp_path / paths.APP_NAME
    assert not old.exists(), "旧名目录应已被改名，不该两份并存"
    assert got.joinpath("config.json").read_text(encoding="utf-8") == '{"api_key":"sk-old"}'
    assert got.joinpath("cache", "translations.json").read_text(encoding="utf-8") == '{"hit":1}'


def test_new_user_gets_new_name_without_legacy(tmp_path):
    """全新用户：没有旧目录，直接用新名，不报错。"""
    assert _adopt_legacy_dir(tmp_path) == tmp_path / paths.APP_NAME


def test_existing_new_dir_wins_and_legacy_untouched(tmp_path):
    """已经迁移过（或两份并存）：认新名，绝不去动旧目录里的东西。"""
    old = _seed_legacy(tmp_path)
    new = tmp_path / paths.APP_NAME
    new.mkdir()
    (new / "config.json").write_text('{"api_key":"sk-new"}', encoding="utf-8")

    got = _adopt_legacy_dir(tmp_path)

    assert got == new
    assert got.joinpath("config.json").read_text(encoding="utf-8") == '{"api_key":"sk-new"}'
    assert old.joinpath("config.json").exists(), "旧目录不该被删或覆盖"


def test_failed_rename_keeps_using_legacy_dir(tmp_path, monkeypatch):
    """改名失败（无权限/被占用/跨卷）→ 就地继续用旧目录。
    名字不一致只是难看，丢 Key 和缓存是事故。"""
    old = _seed_legacy(tmp_path)

    def boom(self, target):
        raise OSError("permission denied")
    monkeypatch.setattr(Path, "rename", boom)

    got = _adopt_legacy_dir(tmp_path)

    assert got == old, "搬不动就该回退到旧目录，而不是指向一个空的新目录"
    assert got.joinpath("config.json").read_text(encoding="utf-8") == '{"api_key":"sk-old"}'
