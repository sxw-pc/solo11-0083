"""Repository-root launcher shim for ``python -m termharvest.cli``.

The real package lives in ``src/termharvest``. This shim keeps the documented
one-command workflow working from a source checkout without installation.
"""

from pathlib import Path

__path__ = [str(Path(__file__).resolve().parent.parent / "src" / "termharvest")]
__version__ = "0.1.0"
AUTHORIZED_USE_NOTICE = "仅可用于本人所有或已获书面授权的本地模拟/测试环境。" "本项目只读采集，不连接真实主机，不写入业务终端。"

__all__ = ["__version__", "AUTHORIZED_USE_NOTICE"]
