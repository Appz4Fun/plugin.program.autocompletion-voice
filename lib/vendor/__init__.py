# -*- coding: utf8 -*-
"""
Vendor directory loader.

AUDIT RESULT (ADR-005): google-genai dependency tree contains native-code
packages that cannot be vendored into the addon repository (constraint C1):
  - cffi (C extension, required by cryptography)
  - pydantic-core (Rust extension, required by pydantic v2)

Decision: vendoring SKIPPED. GeminiSTTProvider uses stdlib urllib/http.client
to call the Gemini REST API directly — zero third-party dependencies required.

If this directory gains pure-Python vendored packages in the future, the
sys.path insert below makes them importable without any changes to callers.
sys.path manipulation occurs at module top level (P-004) before any vendor
imports downstream.
"""

import os
import sys

_VENDOR_DIR = os.path.dirname(os.path.abspath(__file__))

if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)
