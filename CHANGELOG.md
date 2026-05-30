# Changelog

All notable changes to this project will be documented in this file.

## [1.2.0] - 2026-05-30

### Fixed
- Wear calculation now respects skin-specific available wear levels
- MAC-10 | Sakkaku and similar limited-wear skins now show correct quality
- Added wear degradation logging for diagnostics

### Changed
- Increased MARKET_CACHE_VERSION to 4 (cache invalidation)
- Added `get_available_wears()` method to CS2Database
- Updated `_determine_wear_from_float()` to accept `available_wears` parameter
- Added `clear_outcomes_cache()` method to ContractCalculator
