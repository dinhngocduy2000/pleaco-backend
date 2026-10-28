"""Explicit opt-in: these tests create and delete their own broker resources."""


def pytest_addoption(parser):
    parser.addoption("--rabbitmq-test-url", default=None, help="AMQP URL of a disposable test broker")
    parser.addoption("--rabbitmq-test-management-url", default=None, help="Management URL of that test broker")
