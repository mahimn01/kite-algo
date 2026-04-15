"""Entry point for `python -m kite_algo` — delegates to the trading-engine CLI."""

from kite_algo.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
