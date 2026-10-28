"""Manual CLI only; no runner imports or invokes this sender."""
from andy_trader.polymarket_sender import main

if __name__ == '__main__':
    raise SystemExit(main())
