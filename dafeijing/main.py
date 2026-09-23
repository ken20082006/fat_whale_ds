"""進入點。

啟動方式：
    python -m dafeijing.main
"""

from __future__ import annotations

import logging

from telegram import Update

from .bot.app import build_application, create_services
from .logging_setup import setup_logging
from .settings import get_settings

logger = logging.getLogger(__name__)


def main() -> None:
    cfg = get_settings()
    cfg.ensure_dirs()
    setup_logging(cfg.log_dir)

    logger.info("大肥鯨準備下水…")
    services = create_services(cfg)
    application = build_application(services)

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
