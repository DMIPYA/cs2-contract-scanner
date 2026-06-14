from typing import List, Dict, Optional, Tuple
import logging

logger = logging.getLogger('calculator.price_lookup')


class _PriceLookupMixin:
    """Mixin for skin price search with 4-step fallback chain.

    Expects the host class to have:
        - self._cached_get_price_with_float(...)
        - self._strict_input_float: bool
    """

    def _get_skin_price_info(
        self,
        skin_name: str,
        *,
        max_float: Optional[float] = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = True,
        allow_refresh: bool = False,
    ) -> Optional[Tuple[float, Optional[float], str]]:
        """4-step fallback chain for skin price search.

        Steps:
          1. strict_name_match=True + max_float (if max_float is set)
          2. strict_name_match=False + max_float (if max_float is set)
          3. strict_name_match=True + max_float=None
          4. strict_name_match=False + max_float=None

        Returns:
            (price, skin_float, wear) or None if no step yielded a result.
        """
        price_info: Optional[Tuple[float, Optional[float], str]] = None

        # Steps 1-2: with max_float
        if max_float is not None:
            # Step 1: strict match + max_float
            price_info = self._cached_get_price_with_float(
                skin_name,
                max_float=max_float,
                exclude_stattrak=exclude_stattrak,
                require_stattrak=require_stattrak,
                strict_name_match=True,
                allow_refresh=allow_refresh,
            )
            # Step 2: relaxed match + max_float
            if not price_info:
                price_info = self._cached_get_price_with_float(
                    skin_name,
                    max_float=max_float,
                    exclude_stattrak=exclude_stattrak,
                    require_stattrak=require_stattrak,
                    strict_name_match=False,
                    allow_refresh=allow_refresh,
                )

        # Steps 3-4: without max_float
        if not price_info:
            # Step 3: strict match + max_float=None
            price_info = self._cached_get_price_with_float(
                skin_name,
                target_wear=None,
                max_float=None,
                exclude_stattrak=exclude_stattrak,
                require_stattrak=require_stattrak,
                strict_name_match=True,
                allow_refresh=allow_refresh,
            )
        if not price_info:
            # Step 4: relaxed match + max_float=None
            price_info = self._cached_get_price_with_float(
                skin_name,
                target_wear=None,
                max_float=None,
                exclude_stattrak=exclude_stattrak,
                require_stattrak=require_stattrak,
                strict_name_match=False,
                allow_refresh=allow_refresh,
            )

        return price_info

    def _filter_skin_by_float(
        self,
        price_info: Tuple[float, Optional[float], str],
        *,
        max_float: Optional[float] = None,
    ) -> bool:
        """Check if the skin passes float/wear filtering.

        Considers self._strict_input_float.
        Returns True if the skin is suitable, False if it should be excluded.
        """
        _price, skin_float, wear = price_info

        if self._strict_input_float and skin_float is None:
            return False

        if skin_float is None and max_float is not None and float(max_float) < 0.999:
            allowed_wears = ["Factory New", "Minimal Wear"]
            if float(max_float) >= 0.38:
                allowed_wears.append("Field-Tested")
            if wear not in allowed_wears:
                return False

        if max_float is not None and skin_float is not None and float(skin_float) > float(max_float):
            return False

        if self._strict_input_float and skin_float is None:
            return False

        return True

    def _build_skin_entry(
        self,
        skin_name: str,
        collection: str,
        price_info: Tuple[float, Optional[float], str],
        rarity: str,
        *,
        outcomes_count: Optional[int] = None,
        efficiency: float = 0.0,
    ) -> Dict:
        """Build skin dict from price_info."""
        price, skin_float, wear = price_info
        entry: Dict = {
            'name': skin_name,
            'collection': collection,
            'price': float(price) if price is not None else 0.0,
            'float': float(skin_float) if skin_float is not None else None,
            'wear': wear,
            'rarity': rarity,
        }
        if outcomes_count is not None:
            entry['outcomes_count'] = int(outcomes_count)
            entry['efficiency'] = efficiency
        return entry
