"""Andy Trader: a calibrated-forecast harness for crypto directional prediction.

The evaluation layer exists before any strategy does. Every prediction is
written to durable storage before its outcome can be known, and settled later by
a job that never reads what was predicted. That ordering is the point of the
project and cannot be retrofitted.
"""

__version__ = "0.1.0"
