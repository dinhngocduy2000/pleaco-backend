"""Run robot-action tests with synthetic settings and no secret sources.

Pass --integration to enable the disposable-service roundtrip tests on local
RabbitMQ 5673, Redis 6380, and MQTT 1884. Never point them at application services.
"""

import os
from pathlib import Path
import runpy
import sys


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if "--integration" in arguments:
        arguments.remove("--integration")
        os.environ["PLEACO_ROBOT_ACTION_INTEGRATION"] = "1"
    sys.argv = [str(Path(__file__).with_name("test_rabbitmq.py")), *(
        arguments or ["tests/unit/test_robot_action.py", "tests/unit/test_robot_action_topic.py"]
    )]
    runpy.run_path(sys.argv[0], run_name="__main__")
