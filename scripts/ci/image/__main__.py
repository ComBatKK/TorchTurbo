"""Package entrypoint for ``python -m scripts.ci.image``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
