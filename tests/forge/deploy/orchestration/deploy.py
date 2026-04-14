from projects.core.library import env, config, run

import pathlib
import logging

logger = logging.getLogger(__name__)


def init():
    env.init()
    run.init()
    config.init(pathlib.Path(__file__).parent)


def deploy():
    logger.info("hello world")
