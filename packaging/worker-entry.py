"""One fixed stdio worker entrypoint, plus explicit inert resource diagnostic."""

import json
import sys

if sys.argv[1:] == ["--package-selfcheck"]:
    from threadrelay.package_resources import verify_resources

    try:
        print(json.dumps(verify_resources()))
    except Exception:
        print("Packaged resources unavailable.", file=sys.stderr)
        raise SystemExit(2) from None
elif sys.argv[1:]:
    raise SystemExit(2)
else:
    from threadrelay.packaged_worker import main

    raise SystemExit(main())
