"""Serial Party's text-console client API."""

from .client import Serial, SerialBusyError, SerialException, SerialTimeoutException

__all__ = ["Serial", "SerialBusyError", "SerialException", "SerialTimeoutException"]
__version__ = "0.1.0"
