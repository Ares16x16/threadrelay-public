"""Native testing is opt-in, separate from all synthetic source validation."""


def pytest_addoption(parser):
    group = parser.getgroup("threadrelay native acceptance")
    group.addoption(
        "--tdlib-native",
        action="store_true",
        help="Explicit native library execution opt-in",
    )
    group.addoption(
        "--tdlib-live",
        action="store_true",
        help="Explicit controlled native/live fixture authorization",
    )
    group.addoption(
        "--tdlib-fixture",
        help="Private exact fixture JSON; never committed or supplied by ordinary application IPC",
    )
