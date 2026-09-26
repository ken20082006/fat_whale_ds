"""日誌設定：同時輸出到 console 與輪替檔案。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"


def setup_logging(
    log_dir: Path, level: int = logging.INFO, name: str = "fatwhale"
) -> None:
    """`name` 係 log 檔名（不含 .log）。

    Router 一定要傳唔同嘅名：佢同大肥鯨係兩個 process，寫同一個檔嘅話
    兩個 RotatingFileHandler 會爭同一個檔案，輪替時互相截斷，兩邊嘅
    紀錄都會爛。
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    # 避免重複掛 handler（例如測試或重啟時）
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_dir / f"{name}.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 這些套件在 INFO 等級太吵
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)
