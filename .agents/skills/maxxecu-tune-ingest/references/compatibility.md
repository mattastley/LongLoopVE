# Compatibility and validation

The original backend was validated with Python 3.12, lz4 4.4.5 and defusedxml 0.7.1. Observed package fixtures covered serialized MTune versions 1.156–1.160, plus an offline package saved by MTune 1.161.3 (serialized version 1.161). These are historical observations, not a claim about the latest release or all firmware variants.

Observed formats: ZIP packages, bracketed-ID tab/comma logs, LZ4 column-oriented signed-int16 logs, and MaxxECUSettingsFile XML. The original validation compared a paired binary/CSV recording containing 7,784,100 values with exact agreement, and an MTune 1.161.3 offline saved-log readback containing 309,246 values with exact agreement. Private fixtures and generated catalogs are not distributed here, so these comparisons cannot be reproduced from this repository alone.

Synthetic regression tests in `tests/test_maxxecu_backend.py` cover parsing, truncation, unsafe archive paths, protected XML, deduplication, catalog relocation, vehicle corrections and retrieval. They do not establish comprehensive release compatibility.

Unverified: fresh recordings acquired under 1.161.3, older families outside those observations, other binary layouts, every protected-tune implementation, regional CSV layouts, and localized unit conversions. Unsupported/protected content is retained and reported. General units remain unknown. Serialized tune values are not assumed display units. Log/tune capture-time equivalence requires acquisition evidence.
