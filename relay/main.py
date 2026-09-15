from __future__ import annotations

import logging

from dotenv import load_dotenv

from .app import create_app
from .config import RelayConfig


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = RelayConfig.from_env()
    app = create_app(config)
    app.run(host=config.host, port=config.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
