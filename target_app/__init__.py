"""Deliberately hostile legacy credit-union core banking demo app.

Proxy target for computer-use automation exercises. See target_app.app.
"""

from target_app.app import create_app

__all__ = ["create_app"]
