import logging
import logging.config
import colorlog
import configparser
from datetime import datetime

log_file_name = datetime.now().strftime("../logs/option_trader_%Y-%m-%d_%H-%M-%S.log")


def setup_logging():
    # Load logging configuration
    config = configparser.RawConfigParser()
    config.read("logging.conf")

    # Define colorized format
    color_formatter = colorlog.ColoredFormatter(
        "%(log_color)s[%(levelname)s] %(asctime)s - %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'bold_red',
        }
    )

    # Load config and inject dynamic filename
    logging.config.fileConfig(
        'logging.conf',
        defaults={"logfilename": log_file_name},
        disable_existing_loggers=False
    )

    # Replace the dummy formatter for the console handler
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            handler.setFormatter(color_formatter)
