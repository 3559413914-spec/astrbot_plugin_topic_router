"""把插件打包成 AstrBot 能正确安装的 zip。

为什么不用 Windows 的 `Compress-Archive`：它会用反斜杠写条目名
（`astrbot_plugin_topic_router\\detection.py`）。Linux 上 `os.path.normpath`
不把反斜杠当分隔符（它是合法文件名字符），于是解包后得不到预期的目录结构，
AstrBot 的收尾步骤 `_finalize_extracted_archive` 会在 `os.listdir(插件目录)` 上
抛 FileNotFoundError。这里用 zipfile 手工写正斜杠条目，顺便保证：

* 条目名一律用 `/`；
* 目录条目带结尾 `/`，让 `_resolve_archive_root_dir` 能识别出根目录；
* 不包含 `tests/` 子目录 —— 有子目录时 update_dir 会退化成整个插件根目录，
  收尾步骤会把目录往它自己里面搬。

用法：
    python build_zip.py [输出路径]
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
PACKAGE_NAME = "astrbot_plugin_topic_router"
# 打进包里的内容；tests/ 只在本地跑，不进安装包
INCLUDE_FILES = (
    "__init__.py",
    "main.py",
    "router.py",
    "detection.py",
    "state.py",
    "metadata.yaml",
    "_conf_schema.json",
    "README.md",
    "CHANGELOG.md",
    "requirements.txt",
    "LICENSE",
)
EXCLUDE_DIRS = {"__pycache__", "tests", ".git"}


def read_version() -> str:
    """从 metadata.yaml 读版本号，用于拼输出文件名。

    Returns:
        版本号，读不到时用 `dev`。
    """
    text = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("version:"):
            value = line.split(":", 1)[1].strip().strip("'\"")
            return value.lstrip("v") or "dev"
    return "dev"


def default_output() -> Path:
    """默认输出路径：桌面。

    走过的坑都记在这，别再踩：

    * 放在 `F:\\deep seek` 根下或插件目录内，zip 会被这个工作区的清理规则扫走
      （文件名以 `astrbot_` 开头、或就在插件目录里，实测都会消失）；
    * 放在插件目录内还会让"重新打包"把上一个包一起压进去。

    桌面在工作区之外，实测能稳定留下。包**内部**的目录名仍然是
    `astrbot_plugin_topic_router/`，压缩包本身叫什么不影响 AstrBot 识别
    （它看 metadata.yaml 里的 `name`）。

    Returns:
        输出路径。
    """
    return Path.home() / "Desktop" / f"astrbot_plugin_topic_router-{read_version()}.zip"


def check_version_consistency() -> list[str]:
    """检查三处版本信息是否一致。

    版本号曾经漂移过一次：CHANGELOG 写了新版、metadata.yaml 却忘了改，结果打出来的包
    文件名是新版、包里元数据还是旧版。打包前先自检，把这个坑堵掉。

    Returns:
        问题描述列表；空列表表示一致。
    """
    problems: list[str] = []
    version = read_version()

    meta_text = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
    if f"version: v{version}" not in meta_text and f"version: {version}" not in meta_text:
        problems.append(f"metadata.yaml 里的 version 不是 v{version}")

    changelog = PLUGIN_DIR / "CHANGELOG.md"
    if changelog.is_file():
        if f"## v{version}" not in changelog.read_text(encoding="utf-8"):
            problems.append(f"CHANGELOG.md 顶部没有 ## v{version} 小节")

    readme = PLUGIN_DIR / "README.md"
    if readme.is_file():
        text = readme.read_text(encoding="utf-8")
        # 只挑"本插件的版本号"来比：README 里还会提到 AstrBot 自身的版本（如 v4.28.1），
        # 那些不能算不一致。约定写法是「话题路由器 vX.Y.Z」或「插件 vX.Y.Z」。
        for found in set(re.findall(r"(?:话题路由器|插件)\s*v(\d+\.\d+\.\d+)", text)):
            if found != version:
                problems.append(f"README.md 里出现了本插件的其它版本号 v{found}")
                break
    return problems


def build(output: Path) -> Path:
    """写出 zip。

    Args:
        output: 输出路径。

    Returns:
        实际写出的路径。
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    missing = [name for name in INCLUDE_FILES if not (PLUGIN_DIR / name).is_file()]
    if missing:
        raise SystemExit(f"缺少文件，无法打包：{', '.join(missing)}")

    problems = check_version_consistency()
    if problems:
        raise SystemExit(
            "版本信息不一致，先改齐再打包：\n  - " + "\n  - ".join(problems)
        )

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # 目录条目是必需的：AstrBot 靠它 + 子条目前缀来推断"压缩包里的根目录"，
        # 也靠它让解包真的建出这一层目录。缺了它，收尾步骤会在
        # os.listdir(<target>/<包名>) 上抛 FileNotFoundError。
        zf.writestr(f"{PACKAGE_NAME}/", b"")
        for name in INCLUDE_FILES:
            zf.writestr(
                f"{PACKAGE_NAME}/{name}",
                (PLUGIN_DIR / name).read_bytes(),
            )
    return output


def main() -> int:
    """命令行入口。

    Returns:
        退出码。
    """
    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else default_output()
    build(output)
    print(f"写出 {output} ({output.stat().st_size} bytes)")
    with zipfile.ZipFile(output) as zf:
        for info in zf.infolist():
            print(f"  {info.filename}  {info.file_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
