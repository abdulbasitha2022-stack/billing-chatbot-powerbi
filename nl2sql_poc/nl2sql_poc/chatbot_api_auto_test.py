#!/usr/bin/env python3
"""Backwards-compatible entry point: the implementation now lives in chatbot_api.py.

`python chatbot_api_auto_test.py` keeps working and starts the same server.
"""
from chatbot_api import create_app, main  # noqa: F401

if __name__ == "__main__":
    main()
