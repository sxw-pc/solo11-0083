"""termharvest - offline, read-only character-terminal screen harvesting.

This package operates only against loopback addresses and synthetic fixtures.
Use it solely on systems you own or are explicitly authorized to test.
"""

__version__ = "0.1.0"

AUTHORIZED_USE_NOTICE = "仅可用于本人所有或已获书面授权的本地模拟/测试环境。" "本项目只读采集，不连接真实主机，不写入业务终端。"

__all__ = ["__version__", "AUTHORIZED_USE_NOTICE"]
