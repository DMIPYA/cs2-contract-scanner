import os
import time
import json
import gzip
import pickle
import random
import threading
import re
from typing import Dict, List, Optional, Tuple, Any
from dotenv import load_dotenv
import logging
import requests

logger = logging.getLogger(__name__)

# Lightweight aggregated profiling for sales history fetching (full-history)
# Controlled via env SALES_PROFILE=1
_sales_profile = {
    'cache_hit': 0,
    'cache_miss_no_refresh': 0,
    'fetch_ok': 0,
    'fetch_err': 0,
    'fetch_time_s': 0.0,
    'fetch_time_net_s': 0.0,
    'fetch_time_cache_s': 0.0,
}
_sales_profile_lock = threading.Lock()


class MarketCSGOClient:
    """Клиент для работы с Market.CSGO Full Export API с поддержкой float данных"""
    
    def __init__(self):
        load_dotenv()
        self.api_key = os.getenv('MARKET_API_KEY', '')
        self.v2_prices_url = os.getenv('MARKET_CSGO_API_URL', 'https://market.csgo.com/api/v2/prices/USD.json')
        self.v2_full_history_all_url = os.getenv('MARKET_CSGO_FULL_HISTORY_ALL_URL', 'https://market.csgo.com/api/v2/full-history/all.json')
        self.v2_full_history_item_url_tpl = os.getenv('MARKET_CSGO_FULL_HISTORY_ITEM_URL_TPL', 'https://market.csgo.com/api/v2/full-history/{item_id}.json')
        self.base_url = 'https://market.csgo.com/api/full-export'
        self.cache_ttl = int(os.getenv('CACHE_TTL', 300))  # 5 минут по умолчанию
        self.files_to_load = int(os.getenv('FULL_EXPORT_FILES_TO_LOAD', '0'))

        self.sales_history_ttl_seconds = int(os.getenv('MARKET_SALES_HISTORY_TTL', '3600'))  # 1 час по умолчанию

        self._sales_prof_lock = threading.Lock()
        self._sales_prof = {
            'cache_hit': 0,
            'cache_miss_no_refresh': 0,
            'fetch_ok': 0,
            'fetch_err': 0,
            'fetch_time_s': 0.0,
        }

        self.disk_cache_path = os.getenv('MARKET_PRICES_DISK_CACHE', 'market_prices_cache.pkl.gz')
        self.disk_cache_ttl = int(os.getenv('MARKET_PRICES_DISK_CACHE_TTL', '21600'))  # 6 часов по умолчанию

        self.request_min_interval_seconds = float(os.getenv('MARKET_REQUEST_MIN_INTERVAL', '0.10'))
        self.full_export_file_timeout = int(os.getenv('FULL_EXPORT_FILE_TIMEOUT', '20'))
        self.full_export_file_retries = int(os.getenv('FULL_EXPORT_FILE_RETRIES', '2'))

        try:
            self.http_timeout_seconds = float(os.getenv('MARKET_HTTP_TIMEOUT', '30') or 30)
        except Exception:
            self.http_timeout_seconds = 30.0
        try:
            self.http_retries = int(os.getenv('MARKET_HTTP_RETRIES', '3') or 3)
        except Exception:
            self.http_retries = 3
        try:
            self.http_backoff_base_seconds = float(os.getenv('MARKET_HTTP_BACKOFF_BASE', '0.5') or 0.5)
        except Exception:
            self.http_backoff_base_seconds = 0.5
        
        # Расширенный кэш: {market_hash_name: [(price, float, wear), ...]}
        self._prices_cache: Dict[str, List[Tuple[float, Optional[float], str, bool]]] = {}
        self._prices_cache_lock = threading.RLock()
        self._last_update_time: float = 0
        self._last_request_time: float = 0
        self._total_lots_analyzed: int = 0

        # Кэш для ускорения prefix-match fallback (variant -> list[cache_key])
        # Важно: очищать при обновлении _prices_cache
        self._prefix_match_cache: Dict[str, List[str]] = {}
        self._prefix_match_cache_lock = threading.RLock()

        self._sales_history_ids: Dict[str, int] = {}
        self._sales_history_ids_ts: float = 0.0
        self._sales_history_ids_lock = threading.RLock()
        self._sales_history_cache: Dict[int, Tuple[float, Dict[str, Any]]] = {}
        self._sales_history_cache_lock = threading.RLock()
        
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': 'CS2-Contract-Analyzer/1.0'
        })
        
        # Добавляем API ключ если он есть
        if self.api_key:
            self._session.headers.update({
                'Authorization': f'Bearer {self.api_key}'
            })

        # query param формат ключа (для MarketCSGO это более совместимо)
        self._api_key_params = {'key': self.api_key} if self.api_key else {}

        # Возможные качества скинов
        self.wear_levels = [
            "Factory New",
            "Minimal Wear",
            "Field-Tested",
            "Well-Worn",
            "Battle-Scarred",
        ]


    def _request_json(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
        backoff_base_seconds: Optional[float] = None,
        close_connection: bool = False,
    ) -> Optional[Any]:
        if not url:
            return None

        req_timeout = float(timeout) if timeout is not None else float(self.http_timeout_seconds)
        req_retries = int(retries) if retries is not None else int(self.http_retries)
        backoff = float(backoff_base_seconds) if backoff_base_seconds is not None else float(self.http_backoff_base_seconds)

        merged_headers: Dict[str, str] = {}
        if close_connection:
            merged_headers['Connection'] = 'close'
        if headers:
            merged_headers.update({str(k): str(v) for k, v in headers.items() if k})

        last_exc: Optional[Exception] = None

        for attempt in range(max(1, req_retries)):
            try:
                self._rate_limit()
                resp = self._session.get(
                    url,
                    timeout=req_timeout,
                    params=params,
                    headers=merged_headers or None,
                )
                resp.raise_for_status()
                try:
                    return resp.json()
                except Exception as e:
                    last_exc = e
                    return None
            except Exception as e:
                last_exc = e
                if attempt + 1 < max(1, req_retries):
                    try:
                        time.sleep(max(0.0, backoff) * (2 ** attempt))
                    except Exception:
                        pass
                continue

        if last_exc is not None:
            try:
                logger.info('HTTP request failed: url=%s err=%s', str(url), f"{type(last_exc).__name__}: {last_exc}")
            except Exception:
                pass
        return None

    def _build_market_item_name(self, skin_name: str, *, target_wear: Optional[str], require_stattrak: bool) -> Optional[str]:
        nm = str(skin_name or '').strip()
        if not nm or not target_wear:
            return None
        prefix = 'StatTrak™ ' if bool(require_stattrak) else ''
        return f"{prefix}{nm} ({str(target_wear).strip()})"

    def _load_sales_history_ids(self, *, allow_refresh: bool = True) -> None:
        now = time.time()
        with self._sales_history_ids_lock:
            age = now - float(self._sales_history_ids_ts or 0.0)
            if self._sales_history_ids and age < float(self.sales_history_ttl_seconds):
                return
        if not allow_refresh:
            return

        try:
            obj = self._request_json(
                self.v2_full_history_all_url,
                params=self._api_key_params,
                timeout=30,
                retries=2,
            )
            if not isinstance(obj, dict):
                return
            hist = obj.get('history') if isinstance(obj, dict) else None
            if not isinstance(hist, dict) or not hist:
                return

            ids: Dict[str, int] = {}
            for k, v in hist.items():
                try:
                    ids[str(k)] = int(v)
                except Exception:
                    continue

            if ids:
                with self._sales_history_ids_lock:
                    self._sales_history_ids = ids
                    self._sales_history_ids_ts = time.time()
        except Exception:
            logger.debug('Failed to refresh sales history ids', exc_info=True)

    def get_sales_history(self, skin_name: str, *, target_wear: Optional[str], exclude_stattrak: bool = True, require_stattrak: bool = False, allow_refresh: bool = True) -> Optional[Dict[str, Any]]:
        item_name = self._build_market_item_name(skin_name, target_wear=target_wear, require_stattrak=bool(require_stattrak))
        if not item_name:
            return None

        self._load_sales_history_ids(allow_refresh=allow_refresh)

        with self._sales_history_ids_lock:
            item_id = self._sales_history_ids.get(item_name)
        if not item_id:
            return None

        now = time.time()
        with self._sales_history_cache_lock:
            cached = self._sales_history_cache.get(int(item_id))
        if cached is not None:
            ts, data = cached
            if now - float(ts) < float(self.sales_history_ttl_seconds):
                try:
                    with self._sales_prof_lock:
                        self._sales_prof['cache_hit'] = int(self._sales_prof.get('cache_hit') or 0) + 1
                except Exception:
                    pass
                return dict(data) if isinstance(data, dict) else None

        if not allow_refresh:
            try:
                with self._sales_prof_lock:
                    self._sales_prof['cache_miss_no_refresh'] = int(self._sales_prof.get('cache_miss_no_refresh') or 0) + 1
            except Exception:
                pass
            return None

        try:
            t0 = time.perf_counter()
            url = str(self.v2_full_history_item_url_tpl).format(item_id=int(item_id))
            obj = self._request_json(
                url,
                params=self._api_key_params,
                timeout=30,
                retries=2,
            )
            if not isinstance(obj, dict):
                return None
            data = obj.get('data')
            if not isinstance(data, dict):
                return None
            with self._sales_history_cache_lock:
                self._sales_history_cache[int(item_id)] = (time.time(), dict(data))
            try:
                dt = float(time.perf_counter() - t0)
                with self._sales_prof_lock:
                    self._sales_prof['fetch_ok'] = int(self._sales_prof.get('fetch_ok') or 0) + 1
                    self._sales_prof['fetch_time_s'] = float(self._sales_prof.get('fetch_time_s') or 0.0) + dt
                    prof = dict(self._sales_prof)
                if str(os.getenv('SALES_PROFILE', '') or '').strip() in {'1', 'true', 'True', 'yes', 'YES'}:
                    tot = int(prof.get('fetch_ok') or 0) + int(prof.get('fetch_err') or 0)
                    if tot > 0 and (tot % 50) == 0:
                        logger.info(
                            'SalesHistory profile: fetch_ok=%s fetch_err=%s cache_hit=%s miss_no_refresh=%s fetch_time_s=%.1f avg_fetch_ms=%.0f',
                            int(prof.get('fetch_ok') or 0),
                            int(prof.get('fetch_err') or 0),
                            int(prof.get('cache_hit') or 0),
                            int(prof.get('cache_miss_no_refresh') or 0),
                            float(prof.get('fetch_time_s') or 0.0),
                            (1000.0 * float(prof.get('fetch_time_s') or 0.0) / max(1, tot)),
                        )
            except Exception:
                pass
            return dict(data)
        except Exception:
            try:
                with self._sales_prof_lock:
                    self._sales_prof['fetch_err'] = int(self._sales_prof.get('fetch_err') or 0) + 1
            except Exception:
                pass
            logger.debug('Failed to fetch sales history for %s', str(item_name), exc_info=True)
            return None

    def _load_disk_cache(self, *, allow_stale: bool = False) -> Optional[Dict[str, List[Tuple[float, Optional[float], str, bool]]]]:
        path = str(self.disk_cache_path or '').strip()
        if not path:
            return None
        try:
            if not os.path.isabs(path):
                path = os.path.join(os.path.dirname(__file__), path)
            if not os.path.exists(path):
                return None
            st = os.stat(path)
            try:
                logger.info(
                    'Disk price cache found: path=%s size_bytes=%s mtime=%s',
                    str(path),
                    int(getattr(st, 'st_size', 0) or 0),
                    float(getattr(st, 'st_mtime', 0.0) or 0.0),
                )
            except Exception:
                pass
            age = time.time() - float(st.st_mtime)
            if age > float(self.disk_cache_ttl) and (not bool(allow_stale)):
                return None

            with gzip.open(path, 'rb') as f:
                obj = pickle.load(f)
            if not isinstance(obj, dict) or not obj:
                return None
            # Sanitize anomalous prices: collect all prices and remove entries that are
            # absurdly above median (e.g. stale CSFloat data, test entries, corrupted lots).
            try:
                all_prices_flat = []
                for lots in obj.values():
                    if isinstance(lots, list):
                        for lot in lots:
                            if isinstance(lot, (list, tuple)) and len(lot) >= 1:
                                try:
                                    all_prices_flat.append(float(lot[0]))
                                except Exception:
                                    pass
                if all_prices_flat:
                    all_prices_flat.sort()
                    median_price = all_prices_flat[len(all_prices_flat) // 2]
                    # Allow up to 10000x median or $10000 whichever is larger — clearly anomalous prices way above this
                    max_sane_price = max(float(median_price) * 10000.0, 10000.0)
                    cleaned = {}
                    removed = 0
                    for k, lots in obj.items():
                        clean_lots = []
                        if isinstance(lots, list):
                            for lot in lots:
                                try:
                                    if float(lot[0]) > max_sane_price:
                                        removed += 1
                                        continue
                                except Exception:
                                    pass
                                clean_lots.append(lot)
                        cleaned[k] = clean_lots
                    if removed > 0:
                        logger.warning('Disk cache sanitized: removed %d anomalous lots (median=%.2f max_sane=%.0f)', removed, median_price, max_sane_price)
                    obj = cleaned
            except Exception as e:
                logger.info('Disk cache sanitize error: %s', e)
            if age > float(self.disk_cache_ttl) and bool(allow_stale):
                logger.warning('Loaded stale disk price cache (age_s=%.1f > ttl_s=%.1f)', float(age), float(self.disk_cache_ttl))
            return obj
        except Exception as e:
            logger.info(f"Disk cache load failed: {e}")
            return None

    def _save_disk_cache(self, cache_obj: Dict[str, List[Tuple[float, Optional[float], str, bool]]]) -> None:
        path = str(self.disk_cache_path or '').strip()
        if not path:
            return
        try:
            if not os.path.isabs(path):
                path = os.path.join(os.path.dirname(__file__), path)
            tmp_path = f"{path}.tmp"
            with gzip.open(tmp_path, 'wb') as f:
                pickle.dump(cache_obj, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp_path, path)
            try:
                st = os.stat(path)
                logger.info(
                    'Disk price cache saved: path=%s size_bytes=%s mtime=%s',
                    str(path),
                    int(getattr(st, 'st_size', 0) or 0),
                    float(getattr(st, 'st_mtime', 0.0) or 0.0),
                )
            except Exception:
                pass
        except Exception as e:
            logger.info(f"Disk cache save failed: {e}")

    def _reset_prefix_cache(self) -> None:
        with self._prefix_match_cache_lock:
            self._prefix_match_cache = {}

    def _get_prefix_matches(self, variant: str, cache_snapshot: Dict[str, List[Tuple[float, Optional[float], str, bool]]]) -> List[str]:
        v = str(variant or '')
        if not v:
            return []
        with self._prefix_match_cache_lock:
            cached = self._prefix_match_cache.get(v)
        if cached is not None:
            return list(cached)

        matches: List[str] = []
        # Сканы по всем ключам дорогие, но делаем их один раз на variant
        for cache_name in cache_snapshot.keys():
            try:
                if cache_name.startswith(v) or v.startswith(cache_name):
                    matches.append(cache_name)
            except Exception:
                continue

        with self._prefix_match_cache_lock:
            self._prefix_match_cache[v] = list(matches)
        return list(matches)

    def _load_prices_v2(self) -> Optional[Dict[str, List[Tuple[float, Optional[float], str, bool]]]]:
        """Быстрая загрузка цен одним запросом через v2/prices."""
        # Иногда market.csgo.com рвет TLS-сессию (SSLEOFError). Принудительно закрываем соединение.
        data = self._request_json(
            self.v2_prices_url,
            timeout=60,
            params=self._api_key_params,
            close_connection=True,
            retries=3,
        )
        if not isinstance(data, dict):
            return None

        raw_items = data.get('items')
        if not raw_items:
            return None

        # API может возвращать items как список [{market_hash_name, price, ...}]
        # или как словарь {name: {price: ...}} — обрабатываем оба варианта.
        if isinstance(raw_items, list):
            items_iter = raw_items
        elif isinstance(raw_items, dict):
            # Конвертируем старый формат в единый вид
            items_iter = [
                {'market_hash_name': k, 'price': v.get('price') if isinstance(v, dict) else v}
                for k, v in raw_items.items()
            ]
        else:
            logger.warning("v2 prices: неизвестный формат items (%s)", type(raw_items))
            return None

        new_cache: Dict[str, List[Tuple[float, Optional[float], str, bool]]] = {}
        total_lots = 0

        for item_data in items_iter:
            if not isinstance(item_data, dict):
                continue

            item_name = str(item_data.get('market_hash_name') or '').strip()
            if not item_name:
                continue

            # Цена может быть строкой "0.34" или числом
            price_raw = item_data.get('price')
            if price_raw is None:
                continue

            try:
                price = float(str(price_raw).replace('$', '').strip())
            except Exception:
                continue

            if price <= 0:
                continue

            # Souvenir не нужен для trade-up
            if 'souvenir' in item_name.lower():
                continue

            # Выкидываем явно не-скины (стикеры/кейсы/капсулы/агенты и т.п.)
            # Для оружейных скинов почти всегда есть " | " между оружием и паттерном
            if ' | ' not in item_name:
                continue

            # volume = количество активных ордеров на продажу.
            # Сохраняем min(volume, 50) копий, чтобы get_listings возвращала
            # реалистичную глубину ордербука и liquidity-дисконт считался верно.
            try:
                volume = max(1, min(int(str(item_data.get('volume') or '1').strip()), 50))
            except Exception:
                volume = 1

            is_stattrak = 'stattrak' in item_name.lower()
            wear = self._determine_wear(item_name)
            normalized_name = self._normalize_skin_name(item_name)

            if not normalized_name:
                continue

            if normalized_name not in new_cache:
                new_cache[normalized_name] = []

            # Используем верхнюю границу wear диапазона как приблизительный float.
            # Это важно для корректной нормализации: midpoint (0.11 для MW) на скинах
            # с нестандартным min_float (например 0.06) даёт слишком низкую норму и
            # приводит к неправильному определению качества выходных скинов.
            # Верхняя граница гарантирует что нормализованное значение соответствует
            # реальному максимально возможному float для данного wear.
            _WEAR_FLOAT_MAX = {
                'Factory New':   0.0699,
                'Minimal Wear':  0.1499,
                'Field-Tested':  0.3799,
                'Well-Worn':     0.4499,
                'Battle-Scarred': 0.9999,
            }
            item_float = _WEAR_FLOAT_MAX.get(wear, None)

            for _ in range(volume):
                new_cache[normalized_name].append((price, item_float, wear, is_stattrak))
            total_lots += volume

        if not new_cache:
            return None

        logger.info(f"v2 prices: загружено {len(new_cache)} уникальных предметов")
        logger.info(f"v2 prices: всего записей {total_lots}")
        return new_cache
    
    def _is_cache_valid(self) -> bool:
        """Проверка актуальности кэша"""
        return (time.time() - self._last_update_time) < self.cache_ttl
    
    def _rate_limit(self):
        """Ограничение частоты запросов"""
        current_time = time.time()
        min_interval = float(self.request_min_interval_seconds) if self.request_min_interval_seconds is not None else 0.1
        if min_interval < 0.0:
            min_interval = 0.0
        if current_time - self._last_request_time < min_interval:
            time.sleep(min_interval)
        self._last_request_time = time.time()
    
    def _normalize_skin_name(self, skin_name: str) -> str:
        """Агрессивная нормализация имени скина для максимального маппинга"""
        if not skin_name:
            return skin_name
        
        # Удаляем артефакты и приводим к нижнему регистру
        normalized = skin_name.lower().strip()
        
        # Удаляем лишние пробелы и символы
        normalized = re.sub(r'\s+', ' ', normalized)
        normalized = re.sub(r'[^\w\s\|\-★]', '', normalized)
        
        # Удаляем суффиксы качества
        quality_suffixes = [
            r'\s*\(factory new\)',
            r'\s*\(minimal wear\)',
            r'\s*\(field-tested\)',
            r'\s*\(well-worn\)',
            r'\s*\(battle-scarred\)',
            r'\s*\(fn\)',
            r'\s*\(mw\)',
            r'\s*\(ft\)',
            r'\s*\(ww\)',
            r'\s*\(bs\)'
        ]
        
        for suffix in quality_suffixes:
            normalized = re.sub(suffix, '', normalized, flags=re.IGNORECASE)

        # В некоторых выгрузках качество идёт без скобок в конце строки
        normalized = re.sub(
            r'\s+(factory new|minimal wear|field-tested|well-worn|battle-scarred)\s*$',
            '',
            normalized,
            flags=re.IGNORECASE,
        )
        
        # Удаляем StatTrak и ★
        normalized = re.sub(r'stattrak™?\s*', '', normalized, flags=re.IGNORECASE)
        normalized = re.sub(r'★\s*', '', normalized, flags=re.IGNORECASE)
        
        return normalized.strip()
    
    def _generate_search_variants(self, skin_name: str) -> List[str]:
        """Генерирует варианты для поиска скина"""
        variants = []
        base_name = self._normalize_skin_name(skin_name)
        
        # Базовый вариант
        variants.append(base_name)
        
        # Вариант с заменой | на ::
        if '|' in base_name:
            variants.append(base_name.replace('|', '::'))
        
        # Вариант без | совсем
        if '|' in base_name:
            variants.append(base_name.replace('|', ''))
        
        # Вариант с заменой пробелов на _
        variants.append(base_name.replace(' ', '_'))
        
        # Вариант с заменой - на _
        variants.append(base_name.replace('-', '_'))
        
        # Вариант с заменой | на пробел
        if '|' in base_name:
            variants.append(base_name.replace('|', ' '))
        
        # Вариант с несколькими пробелами на один
        variants.append(re.sub(r'\s+', ' ', base_name))

        # Приводим все варианты к единому виду (один пробел, trim)
        cleaned: List[str] = []
        for v in variants:
            if not v:
                continue
            v2 = re.sub(r'\s+', ' ', str(v)).strip()
            if v2:
                cleaned.append(v2)

        return list(set(cleaned))  # Удаляем дубликаты
    
    def load_prices(self, force_refresh: bool = False) -> bool:
        """Загрузка цен из Full Export API с двухэтапной загрузкой"""
        if not force_refresh and self._is_cache_valid():
            logger.info("Используем кэшированные цены")
            return True

        if not force_refresh:
            disk_cache = self._load_disk_cache()
            if disk_cache:
                with self._prices_cache_lock:
                    self._prices_cache = disk_cache
                    self._last_update_time = time.time()
                    self._total_lots_analyzed = sum(len(v) for v in disk_cache.values())
                self._reset_prefix_cache()
                logger.info("Кэш цен успешно загружен с диска")
                return True

        # Сначала пробуем быстрый v2 прайс-лист (1 запрос вместо множества файлов)
        v2_cache = self._load_prices_v2()
        if v2_cache:
            with self._prices_cache_lock:
                self._prices_cache = v2_cache
                self._last_update_time = time.time()
                self._total_lots_analyzed = sum(len(v) for v in v2_cache.values())
            self._reset_prefix_cache()
            self._save_disk_cache(v2_cache)
            logger.info("Кэш цен успешно обновлен (v2)")
            return True
        
        try:
            logger.info("Загрузка цен из API...")
            
            # Этап 1: Получение списка файлов
            data = self._request_json(
                f"{self.base_url}/USD.json",
                timeout=30,
                params=self._api_key_params,
                retries=3,
            )
            if not isinstance(data, dict):
                raise RuntimeError('Invalid JSON response (expected dict)')
            
            if 'items' not in data or not data['items']:
                logger.error("Неверный формат ответа API - отсутствуют items")
                # Если уже есть рабочий кэш — не затираем его моковыми ценами
                if self._prices_cache:
                    logger.warning("Оставляем последний успешный кэш цен (API вернул неверный формат)")
                    return False
                return self._load_mock_prices()
            
            items = data['items']

            # По умолчанию грузим все файлы (иначе часть скинов будет без цен)
            if self.files_to_load and self.files_to_load > 0:
                files_to_load = items[: self.files_to_load]
            else:
                files_to_load = items
            logger.info(f"Загрузка {len(files_to_load)} файлов...")
            
            total_lots_processed = 0

            # Собираем новый кэш отдельно, чтобы при ошибке не потерять старый
            with self._prices_cache_lock:
                old_cache = self._prices_cache
                old_last_update_time = self._last_update_time
                old_total_lots_analyzed = self._total_lots_analyzed
            new_cache: Dict[str, List[Tuple[float, Optional[float], str, bool]]] = {}
            
            # Проходим по всем файлам
            for i, file_name in enumerate(files_to_load):
                logger.info(f"Загрузка файла {i+1}/{len(files_to_load)}: {file_name}")
                
                try:
                    file_data = self._request_json(
                        f"{self.base_url}/{file_name}",
                        timeout=int(self.full_export_file_timeout),
                        params=self._api_key_params,
                        close_connection=True,
                        retries=max(1, int(self.full_export_file_retries) + 1),
                        backoff_base_seconds=0.25,
                    )
                    if file_data is None:
                        raise RuntimeError('file download failed')
                    
                    logger.debug(f"Загрузка файла {file_name}, тип данных: {type(file_data)}")
                    
                    lots_in_file = self._parse_file_data(file_data, cache_override=new_cache)
                    total_lots_processed += lots_in_file
                    logger.info(f"Файл {file_name}: обработано {lots_in_file} лотов")
                     
                except Exception as e:
                    logger.error(f"Ошибка при загрузке файла {file_name}: {e}")
                    continue

            # Если не смогли собрать ни одной цены — откатываемся к старому кэшу
            if not new_cache:
                with self._prices_cache_lock:
                    self._prices_cache = old_cache
                    self._last_update_time = old_last_update_time
                    self._total_lots_analyzed = old_total_lots_analyzed
                logger.error("Не удалось загрузить ни одной цены из API; оставляем последний успешный кэш")
                return False
            
            with self._prices_cache_lock:
                self._prices_cache = new_cache
                self._last_update_time = time.time()
                self._total_lots_analyzed = total_lots_processed
            self._reset_prefix_cache()
            self._save_disk_cache(new_cache)
            
            # Логирование результатов
            unique_skins = len(self._prices_cache)
            logger.info(f"Загружено: {unique_skins} уникальных скинов. Цены сконвертированы в USD")
            logger.info(f"Всего лотов проанализировано: {total_lots_processed}")
            logger.info("Кэш цен успешно обновлен")
            
            return True
            
        except Exception as e:
            logger.error(f"Ошибка при загрузке цен: {e}")
            # Не затираем рабочий кэш моковыми ценами при сетевой ошибке
            with self._prices_cache_lock:
                has_cache = bool(self._prices_cache)
            if has_cache:
                logger.warning("Оставляем последний успешный кэш цен (сетевая ошибка)")
                return False
            # If there's a disk cache (even stale) — prefer it over mock prices
            try:
                disk_cache = self._load_disk_cache(allow_stale=True)
                if disk_cache:
                    with self._prices_cache_lock:
                        self._prices_cache = disk_cache
                        self._last_update_time = time.time()
                        self._total_lots_analyzed = sum(len(v) for v in disk_cache.values())
                    self._reset_prefix_cache()
                    logger.warning('Using stale disk cache due to network error (cache_size=%s)', int(len(disk_cache)))
                    return True
            except Exception:
                pass
            return self._load_mock_prices()

    def _parse_file_data(self, file_data: dict, cache_override: Optional[Dict[str, List[Tuple[float, Optional[float], str, bool]]]] = None) -> int:
        """Парсинг данных из одного файла экспорта с отладкой"""
        lots_processed = 0
        target_cache = cache_override if cache_override is not None else self._prices_cache
        
        # Проверяем тип данных
        if isinstance(file_data, list):
            # API возвращает прямой список лотов
            items = file_data
            # Убираем спам логи
            # logger.debug(f"Файл содержит прямой список из {len(items)} лотов")
        elif isinstance(file_data, dict) and 'items' in file_data:
            # API возвращает словарь с items
            items = file_data['items']
            # Убираем спам логи
            # logger.debug(f"Файл содержит словарь с {len(items)} лотов")
        else:
            logger.warning(f"Неизвестный формат данных: {type(file_data)}")
            return 0
        
        # Отладка: выводим структуру первого элемента
        # Убираем спам логи о структуре лотов
        # if items and len(items) > 0:
        #     first_item = items[0]
        #     logger.debug(f"Структура первого лота: {type(first_item)}, длина: {len(first_item) if isinstance(first_item, list) else 'N/A'}")
        #     logger.debug(f"Первый лот: {first_item}")
        
        for item in items:
            try:
                # Проверяем минимальную длину массива
                if len(item) < 11:
                    continue
                
                # Извлекаем базовые данные
                price_raw = item[0]
                market_hash_name = item[2] if len(item) > 2 else ""
                item_float = float(item[10]) if len(item) > 10 and item[10] is not None else None
                
                # Быстрый фильтр мусора: оставляем только оружейные скины
                # (стикеры/капсулы/контейнеры и т.п. не участвуют в trade-up)
                if ' | ' not in market_hash_name:
                    continue

                # По формату full-export поле type обычно ближе к концу и часто строковое
                # (например "Sticker", "Container", "Pistol" ...)
                item_type = None
                if len(item) > 15:
                    item_type = item[15]
                if isinstance(item_type, str) and item_type.lower() in {'sticker', 'container'}:
                    continue

                # Конвертируем цену
                try:
                    raw_val = float(price_raw)

                    # Full-export может отдавать цену как:
                    # - в тысячных доллара (2980 => $2.98)
                    # - в центах (298 => $2.98)
                    # Подбираем делитель по масштабу, чтобы не занижать цены в 10 раз.
                    div = 1000.0 if raw_val >= 1000.0 else 100.0
                    price = raw_val / div
                except (ValueError, TypeError):
                    continue
                
                if price <= 0 or not market_hash_name:
                    continue

                # Souvenir нельзя крафтить/получать через trade-up — исключаем полностью
                if 'souvenir' in str(market_hash_name).lower():
                    continue
                
                # Извлекаем float если есть
                item_float = None
                if len(item) > 10 and item[10] is not None:
                    try:
                        item_float = float(item[10])
                    except (ValueError, TypeError):
                        item_float = None
                
                # Определяем качество: если есть float, используем его (в full-export качество часто не указано в названии)
                wear = self._determine_wear_from_float(item_float) if item_float is not None else self._determine_wear(market_hash_name)

                # Определяем StatTrak по исходному названию (так как нормализация может его вырезать)
                is_stattrak = 'stattrak' in market_hash_name.lower()
                
                # Нормализуем имя для кэширования
                normalized_name = self._normalize_skin_name(market_hash_name)
                
                # Добавляем в кэш
                if normalized_name not in target_cache:
                    target_cache[normalized_name] = []
                
                target_cache[normalized_name].append((price, item_float, wear, is_stattrak))
                lots_processed += 1
                
            except Exception as e:
                # Убираем спам логи об ошибках обработки лотов
                # logger.debug(f"Ошибка обработки лота: {e}")
                continue
        
        # Убираем спам логи о количестве обработанных лотов
        # logger.debug(f"Обработано {lots_processed} лотов из файла")
        return lots_processed
    
    def _determine_wear(self, market_hash_name: str) -> str:
        """Определяет качество скина по названию"""
        name_lower = market_hash_name.lower()
        
        if 'factory new' in name_lower or '(fn)' in name_lower:
            return "Factory New"
        elif 'minimal wear' in name_lower or '(mw)' in name_lower:
            return "Minimal Wear"
        elif 'field-tested' in name_lower or '(ft)' in name_lower:
            return "Field-Tested"
        elif 'well-worn' in name_lower or '(ww)' in name_lower:
            return "Well-Worn"
        elif 'battle-scarred' in name_lower or '(bs)' in name_lower:
            return "Battle-Scarred"
        else:
            return "Unknown"

    def _determine_wear_from_float(self, item_float: float) -> str:
        """Определяет качество по float (если quality нет в названии full-export)."""
        try:
            f = float(item_float)
        except Exception:
            return "Unknown"

        if f < 0.07:
            return "Factory New"
        if f < 0.15:
            return "Minimal Wear"
        if f < 0.38:
            return "Field-Tested"
        if f < 0.45:
            return "Well-Worn"
        return "Battle-Scarred"
    
    def get_price(
        self,
        skin_name: str,
        target_wear: str = None,
        max_float: float = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = True,
        allow_refresh: bool = True,
    ) -> Optional[float]:
        """
        Улучшенный поиск цены с маппингом на разные качества и фильтрацией StatTrak
        
        Args:
            skin_name: имя скина из базы данных (без качества)
            target_wear: целевое качество (опционально)
            max_float: максимальный допустимый float (опционально)
            exclude_stattrak: исключить StatTrak предметы (по умолчанию True)
            
        Returns:
            float: цена или None
        """
        if (not self._is_cache_valid()) and allow_refresh:
            self.load_prices()
        
        # Генерируем все варианты поиска
        search_variants = self._generate_search_variants(skin_name)

        # Берем snapshot кэша, чтобы во время поиска он не поменялся другим потоком
        with self._prices_cache_lock:
            cache_snapshot = self._prices_cache
        
        # Собираем все цены из всех найденных скинов
        all_prices = []

        # 1) Сначала пробуем точные совпадения по ключу кэша (самый надежный вариант)
        for variant in search_variants:
            if variant not in cache_snapshot:
                continue
            for price, item_float, wear, is_stattrak in cache_snapshot[variant]:
                # Фильтр StatTrak
                if exclude_stattrak and is_stattrak:
                    continue
                if require_stattrak and (not is_stattrak):
                    continue

                # Фильтр по качеству
                if target_wear and wear != target_wear:
                    continue

                # Фильтр по float
                if max_float is not None:
                    # Для v2 prices float часто отсутствует (None). В этом случае не выкидываем цену.
                    if item_float is not None and item_float > max_float:
                        continue

                all_prices.append((price, item_float, wear))

        # Если качество запрошено, но в export оно отсутствует/Unknown, то не обнуляем цену.
        # Fallback: берем самый дешевый ордер для точного имени без фильтра по wear.
        if (not all_prices) and target_wear:
            for variant in search_variants:
                if variant not in cache_snapshot:
                    continue
                for price, item_float, wear, is_stattrak in cache_snapshot[variant]:
                    if exclude_stattrak and is_stattrak:
                        continue
                    if require_stattrak and (not is_stattrak):
                        continue

                    if max_float is not None:
                        if item_float is not None and item_float > max_float:
                            continue

                    all_prices.append((price, item_float, wear))

        # 2) Если не нашли, используем более «грязный» prefix-match как fallback
        if (not all_prices) and (not strict_name_match):
            for variant in search_variants:
                for cache_name in self._get_prefix_matches(variant, cache_snapshot):
                    for price, item_float, wear, is_stattrak in cache_snapshot.get(cache_name, []):
                        # Фильтр StatTrak
                        if exclude_stattrak and is_stattrak:
                            continue
                        if require_stattrak and (not is_stattrak):
                            continue
                        
                        # Фильтр по качеству
                        if target_wear and wear != target_wear:
                            continue
                        
                        # Фильтр по float
                        if max_float is not None:
                            if item_float is not None and item_float > max_float:
                                continue
                        
                        all_prices.append((price, item_float, wear))
        
        if (not all_prices) and (not strict_name_match) and target_wear is None and max_float is None:
            # Если ничего не найдено, ищем самую дешевую цену без фильтров
            for variant in search_variants:
                for cache_name in self._get_prefix_matches(variant, cache_snapshot):
                    for price, item_float, wear, is_stattrak in cache_snapshot.get(cache_name, []):
                        if exclude_stattrak and is_stattrak:
                            continue
                        if require_stattrak and (not is_stattrak):
                            continue
                        all_prices.append((price, item_float, wear, is_stattrak))
        
        if not all_prices:
            # Fallback: v2 prices can lack wear variants; don't drop the price entirely.
            if target_wear is not None:
                return self.get_price(
                    skin_name,
                    target_wear=None,
                    max_float=max_float,
                    exclude_stattrak=exclude_stattrak,
                    require_stattrak=require_stattrak,
                    strict_name_match=strict_name_match,
                    allow_refresh=False,
                )
            return None
        
        # Возвращаем самую дешевую цену
        return min(all_prices, key=lambda x: x[0])[0]
    
    def get_price_with_float(
        self,
        skin_name: str,
        target_wear: str = None,
        max_float: float = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = False,
        allow_refresh: bool = True,
    ) -> Optional[Tuple[float, float, str]]:
        """
        Улучшенный поиск цены с float информацией, маппингом и фильтрацией StatTrak
        
        Returns:
            Tuple[float, float, str]: (price, float_value, wear) или None
        """
        if (not self._is_cache_valid()) and allow_refresh:
            self.load_prices()
        
        # Генерируем все варианты поиска
        search_variants = self._generate_search_variants(skin_name)

        # Берем snapshot кэша, чтобы во время поиска он не поменялся другим потоком
        with self._prices_cache_lock:
            cache_snapshot = self._prices_cache
        
        # Собираем все цены из всех найденных скинов
        all_prices = []

        # 1) Точные совпадения по ключу кэша
        for variant in search_variants:
            if variant not in cache_snapshot:
                continue
            for price, item_float, wear, is_stattrak in cache_snapshot[variant]:
                # Фильтр StatTrak
                if exclude_stattrak and is_stattrak:
                    continue
                if require_stattrak and (not is_stattrak):
                    continue

                # Фильтр по качеству
                if target_wear and wear != target_wear:
                    continue

                # Фильтр по float
                if max_float is not None:
                    if item_float is not None and item_float > max_float:
                        continue

                all_prices.append((price, item_float, wear))

        # 2) Fallback: prefix match
        if (not all_prices) and (not strict_name_match):
            for variant in search_variants:
                for cache_name in self._get_prefix_matches(variant, cache_snapshot):
                    for price, item_float, wear, is_stattrak in cache_snapshot.get(cache_name, []):
                        # Фильтр StatTrak
                        if exclude_stattrak and is_stattrak:
                            continue
                        if require_stattrak and (not is_stattrak):
                            continue
                        
                        # Фильтр по качеству
                        if target_wear and wear != target_wear:
                            continue
                        
                        # Фильтр по float
                        if max_float is not None:
                            if item_float is not None and item_float > max_float:
                                continue
                        
                        all_prices.append((price, item_float, wear))
        
        if (not all_prices) and (not strict_name_match) and target_wear is None and max_float is None:
            # Если ничего не найдено, ищем самую дешевую цену без фильтров
            for variant in search_variants:
                for cache_name in self._get_prefix_matches(variant, cache_snapshot):
                    for price, item_float, wear, is_stattrak in cache_snapshot.get(cache_name, []):
                        if exclude_stattrak and is_stattrak:
                            continue
                        if require_stattrak and (not is_stattrak):
                            continue
                        all_prices.append((price, item_float, wear))
        
        if not all_prices:
            if target_wear is not None:
                return self.get_price_with_float(
                    skin_name,
                    target_wear=None,
                    max_float=max_float,
                    exclude_stattrak=exclude_stattrak,
                    require_stattrak=require_stattrak,
                    strict_name_match=strict_name_match,
                    allow_refresh=False,
                )
            return None
        
        # Возвращаем самый дешевый
        cheapest = min(all_prices, key=lambda x: x[0])
        return cheapest[0], cheapest[1], cheapest[2]

    def get_listings(
        self,
        skin_name: str,
        *,
        target_wear: str = None,
        max_float: float = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = False,
        allow_refresh: bool = True,
        limit: int = 50,
    ) -> List[Tuple[float, Optional[float], str]]:
        if (not self._is_cache_valid()) and allow_refresh:
            self.load_prices()

        search_variants = self._generate_search_variants(skin_name)
        with self._prices_cache_lock:
            cache_snapshot = self._prices_cache

        lots: List[Tuple[float, Optional[float], str]] = []

        def _collect_from_key(cache_key: str) -> None:
            for price, item_float, wear, is_stattrak in cache_snapshot.get(cache_key, []):
                if exclude_stattrak and is_stattrak:
                    continue
                if require_stattrak and (not is_stattrak):
                    continue
                if target_wear and wear != target_wear:
                    continue
                if max_float is not None:
                    if item_float is not None and float(item_float) > float(max_float):
                        continue
                lots.append((float(price), item_float, wear))

        for variant in search_variants:
            if variant in cache_snapshot:
                _collect_from_key(variant)

        if (not lots) and (not strict_name_match):
            for variant in search_variants:
                for cache_name in self._get_prefix_matches(variant, cache_snapshot):
                    _collect_from_key(cache_name)

        lots = [x for x in lots if x and float(x[0]) > 0]
        lots.sort(key=lambda x: (float(x[0]), 1.0 if x[1] is None else float(x[1])))
        return lots[: int(max(0, limit))]

    def get_liquidity_metrics(
        self,
        skin_name: str,
        *,
        target_wear: str = None,
        max_float: float = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = False,
        allow_refresh: bool = True,
        depth_n: int = 10,
    ) -> Dict:
        lots = self.get_listings(
            skin_name,
            target_wear=target_wear,
            max_float=max_float,
            exclude_stattrak=exclude_stattrak,
            require_stattrak=require_stattrak,
            strict_name_match=strict_name_match,
            allow_refresh=allow_refresh,
            limit=max(50, int(depth_n) * 5),
        )
        prices = [float(x[0]) for x in lots if x and float(x[0]) > 0]
        prices.sort()
        n = len(prices)
        if n <= 0:
            return {
                'listings_count': 0,
                'min_price': None,
                'p10_price': None,
                'median_price': None,
                'p90_price': None,
                'buy_cost_n': None,
            }

        def _pct(p: float) -> float:
            if n == 1:
                return prices[0]
            idx = int(round((n - 1) * float(p)))
            if idx < 0:
                idx = 0
            if idx >= n:
                idx = n - 1
            return prices[idx]

        depth = min(int(depth_n), n)
        buy_cost_n = float(sum(prices[:depth])) if depth > 0 else None

        return {
            'listings_count': int(n),
            'min_price': float(prices[0]),
            'p10_price': float(_pct(0.10)),
            'median_price': float(_pct(0.50)),
            'p90_price': float(_pct(0.90)),
            'buy_cost_n': float(buy_cost_n) if buy_cost_n is not None else None,
        }

    def get_effective_sell_price(
        self,
        skin_name: str,
        *,
        target_wear: str = None,
        max_float: float = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = False,
        allow_refresh: bool = True,
    ) -> Optional[float]:
        metrics = self.get_liquidity_metrics(
            skin_name,
            target_wear=target_wear,
            max_float=max_float,
            exclude_stattrak=exclude_stattrak,
            require_stattrak=require_stattrak,
            strict_name_match=strict_name_match,
            allow_refresh=allow_refresh,
        )
        n = int(metrics.get('listings_count') or 0)
        p10 = metrics.get('p10_price')
        mn = metrics.get('min_price')
        base_book = float(p10) if p10 is not None else (float(mn) if mn is not None else None)

        try:
            max_listings_for_sales = int(os.getenv('LIQ_MAX_LISTINGS_FOR_SALES', '3') or 3)
        except Exception:
            max_listings_for_sales = 3
        try:
            high_price_for_sales = float(os.getenv('LIQ_HIGH_PRICE_FOR_SALES', '250') or 250)
        except Exception:
            high_price_for_sales = 250.0

        need_sales = bool(allow_refresh) and (
            (n > 0 and n <= int(max_listings_for_sales))
            or (base_book is not None and float(base_book) >= float(high_price_for_sales) and n <= 10)
        )

        sales = None
        if need_sales:
            sales = self.get_sales_history(
                skin_name,
                target_wear=target_wear,
                exclude_stattrak=exclude_stattrak,
                require_stattrak=require_stattrak,
                allow_refresh=True,
            )

        base_sales = None
        base_sales_median = None
        base_sales_median_30d = None
        sales_prices = None
        sales_hist_count = 0
        sales_hist_count_30d = 0
        sales7d = 0
        sales30d = 0
        if isinstance(sales, dict):
            try:
                hist = sales.get('history')
                if isinstance(hist, list) and hist:
                    prices = []
                    prices_30d = []
                    now_ts = time.time()
                    min_ts = now_ts - (30.0 * 24.0 * 3600.0)
                    for it in hist:
                        # docs: [timestamp, price_rub, price_usd, price_eur]
                        try:
                            if isinstance(it, (list, tuple)) and len(it) >= 3:
                                ts = float(it[0])
                                pv = float(it[2])
                            elif isinstance(it, dict):
                                # fallback for any alternative formats
                                ts = float(it.get('time') or it.get('ts') or 0.0)
                                pv = float(it.get('USD') or it.get('price') or 0.0)
                            else:
                                continue
                        except Exception:
                            continue
                        if pv > 0:
                            prices.append(pv)
                            if ts and ts >= min_ts:
                                prices_30d.append(pv)
                    if prices:
                        prices.sort()
                        sales_prices = prices
                        sales_hist_count = int(len(prices))
                        base_sales_median = float(prices[int(len(prices) // 2)])
                    if prices_30d:
                        prices_30d.sort()
                        sales_hist_count_30d = int(len(prices_30d))
                        base_sales_median_30d = float(prices_30d[int(len(prices_30d) // 2)])
            except Exception:
                base_sales_median = None

            try:
                sales7d = int((sales.get('sales7d') or {}).get('USD') or 0)
            except Exception:
                sales7d = 0
            try:
                sales30d = int((sales.get('sales30d') or {}).get('USD') or 0)
            except Exception:
                sales30d = 0
            try:
                avg7 = (sales.get('average7d') or {}).get('USD')
                avg30 = (sales.get('average30d') or {}).get('USD')
                avg_all = (sales.get('average') or {}).get('USD')
                if base_sales_median is not None and base_sales_median > 0:
                    base_sales = float(base_sales_median)
                elif avg7 is not None and sales7d >= 3:
                    base_sales = float(avg7)
                elif avg30 is not None and sales30d >= 5:
                    base_sales = float(avg30)
                elif avg_all is not None:
                    base_sales = float(avg_all)
            except Exception:
                base_sales = None

        min_sales_30d = int(os.getenv('LIQ_MIN_SALES_30D', '30') or 30)
        max_listing_ratio = float(os.getenv('LIQ_MAX_LISTING_TO_MEDIAN_RATIO', '2.0') or 2.0)

        median_ref = None
        if base_sales_median_30d is not None and base_sales_median_30d > 0:
            median_ref = float(base_sales_median_30d)
        elif base_sales_median is not None and base_sales_median > 0:
            median_ref = float(base_sales_median)

        # If we have a per-sale median and either volume is low or order book is far above median,
        # force the reliable baseline to median.
        if median_ref is not None and base_book is not None and base_book > 0:
            vol30 = sales30d if int(sales30d) > 0 else int(sales_hist_count_30d)
            ratio = float(base_book) / float(median_ref) if median_ref > 0 else None
            if (vol30 > 0 and vol30 < int(min_sales_30d)) or (ratio is not None and ratio > float(max_listing_ratio)):
                base_sales = float(median_ref)

        base = base_sales if (base_sales is not None and base_sales > 0) else base_book
        if base is None:
            return None

        if n <= 0:
            return None

        if base_sales is not None and base_sales > 0:
            if sales7d >= 10:
                discount = 1.0
            elif sales7d >= 3:
                discount = 0.90
            elif sales30d >= 10:
                discount = 0.75
            elif sales30d >= 3:
                discount = 0.60
            else:
                discount = 0.40
        else:
            if n < 3:
                discount = 0.35
            elif n < 10:
                discount = 0.60
            elif n < 25:
                discount = 0.80
            else:
                discount = 1.0

        return float(base) * float(discount)
    
    def get_cache_info(self) -> Dict:
        """Получение информации о кэше"""
        return {
            'items_count': len(self._prices_cache),
            'cache_age_seconds': time.time() - self._last_update_time,
            'total_lots_analyzed': self._total_lots_analyzed,
            'unique_skins': len(self._prices_cache)
        }

    def get_order_book(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        require_stattrak: bool = False,
    ) -> Optional[Dict]:
        """
        Fetch best ask and best bid for a skin.
        - best_ask: from v2/prices/USD.json (already in _prices_cache) — real cheapest listing
        - best_bid: from class_instance/USD.json — best buy order
        Returns None if ask price not found.
        """
        st_prefix = 'StatTrak™ ' if bool(require_stattrak) else ''
        if target_wear:
            mhn = f"{st_prefix}{skin_name} ({target_wear})"
        else:
            mhn = f"{st_prefix}{skin_name}"

        # ── best_ask from v2/prices cache (most accurate) ────────────────────
        best_ask = None
        try:
            normalized = self._normalize_skin_name(mhn)
            with self._prices_cache_lock:
                lots = self._prices_cache.get(normalized)
            if lots and target_wear:
                # Filter to matching wear only
                filtered = [float(l[0]) for l in lots if l[0] > 0 and str(l[2]) == target_wear]
                if filtered:
                    best_ask = min(filtered)
            elif lots:
                prices = sorted([float(l[0]) for l in lots if l[0] > 0])
                if prices:
                    best_ask = prices[0]
        except Exception:
            pass

        # Fallback: load fresh from v2/prices if cache empty
        if best_ask is None:
            try:
                v2 = self._load_prices_v2()
                if v2:
                    normalized = self._normalize_skin_name(mhn)
                    lots = v2.get(normalized) or []
                    if lots and target_wear:
                        filtered = [float(l[0]) for l in lots if l[0] > 0 and str(l[2]) == target_wear]
                        if filtered:
                            best_ask = min(filtered)
                    elif lots:
                        prices = sorted([float(l[0]) for l in lots if l[0] > 0])
                        if prices:
                            best_ask = prices[0]
            except Exception:
                pass

        if best_ask is None:
            return None

        # ── best_bid from class_instance ─────────────────────────────────────
        best_bid = None
        try:
            ci_data = self._get_class_instance_prices()
            if ci_data:
                for entry in ci_data.values():
                    if str(entry.get('market_hash_name') or '') == mhn:
                        raw_bid = entry.get('buy_order')
                        if raw_bid is not None:
                            b = float(raw_bid)
                            # Sanity: ignore bids < 1% of ask (stale/junk)
                            if b > 0 and b >= best_ask * 0.01:
                                best_bid = b
                        break
        except Exception:
            pass

        return {'best_ask': best_ask, 'best_bid': best_bid, 'currency': 'USD'}

    def _get_class_instance_prices(self) -> Optional[Dict]:
        """Load and cache /api/v2/prices/class_instance/USD.json (TTL 5 min)."""
        now = time.time()
        with self._prices_cache_lock:
            cached_ts = getattr(self, '_ci_prices_ts', 0.0)
            cached_data = getattr(self, '_ci_prices_data', None)
            if cached_data is not None and (now - float(cached_ts)) < 300.0:
                return cached_data

        url = 'https://market.csgo.com/api/v2/prices/class_instance/USD.json'
        data = self._request_json(url, timeout=30, retries=2)
        if not isinstance(data, dict):
            return None
        items = data.get('items')
        if not isinstance(items, dict) or not items:
            return None

        with self._prices_cache_lock:
            self._ci_prices_data = items
            self._ci_prices_ts = time.time()
        return items

    def suggest_request_price(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        require_stattrak: bool = False,
    ) -> Optional[Dict]:
        """
        Suggest an optimal buy-request price anchored to sales history median.

        Core idea:
          Sellers accept requests priced near what they've actually sold for recently.
          Other buyers won't bother outbidding a non-round price with no obvious upside.
          Result: filled quickly, meaningful discount vs ask, no bot-war incentive.

        Algorithm:
          1. Fetch sales history → compute median of last N sales (USD).
          2. Fetch best_ask and best_bid from class_instance prices.
          3. suggested = median * MC_REQUEST_MEDIAN_RATIO  (default 0.97)
             — 3% below median: seller still gets a fair deal, buyer saves vs ask.
          4. Hard caps:
             a. suggested <= best_ask * MC_REQUEST_MAX_VS_ASK  (default 0.95, always cheaper)
             b. suggested >= best_ask * MC_REQUEST_MIN_VS_ASK  (default 0.70, not insultingly low)
          5. If no sales history: fall back to best_ask * MC_REQUEST_FALLBACK_RATIO (default 0.90).
          6. If no ask price at all: return None.

        Env vars:
          MC_REQUEST_MEDIAN_RATIO    float  0.97   target % of sales median
          MC_REQUEST_MAX_VS_ASK      float  0.95   hard ceiling vs ask
          MC_REQUEST_MIN_VS_ASK      float  0.70   hard floor vs ask (don't lowball)
          MC_REQUEST_FALLBACK_RATIO  float  0.90   ratio of ask when no history
          MC_REQUEST_MIN_SALES       int    5      min sales needed to use median

        Returns dict:
          'suggested_price'  — float
          'best_ask'         — float
          'best_bid'         — float or None
          'sales_median'     — float or None  (median of recent sales)
          'sales_count'      — int            (number of sales used)
          'savings_vs_ask'   — float
          'savings_pct'      — float
        Returns None if no ask price available.
        """
        # ── 1. Order book ────────────────────────────────────────────────────
        book = self.get_order_book(skin_name, target_wear=target_wear, require_stattrak=require_stattrak)
        if not book:
            return None
        best_ask = float(book['best_ask'])
        best_bid = book.get('best_bid')

        # ── 3. Config ────────────────────────────────────────────────────────
        try:
            median_ratio = float(os.getenv('MC_REQUEST_MEDIAN_RATIO', '0.97') or 0.97)
        except Exception:
            median_ratio = 0.97
        try:
            max_vs_ask = float(os.getenv('MC_REQUEST_MAX_VS_ASK', '0.95') or 0.95)
        except Exception:
            max_vs_ask = 0.95
        try:
            min_vs_ask = float(os.getenv('MC_REQUEST_MIN_VS_ASK', '0.70') or 0.70)
        except Exception:
            min_vs_ask = 0.70
        try:
            fallback_ratio = float(os.getenv('MC_REQUEST_FALLBACK_RATIO', '0.90') or 0.90)
        except Exception:
            fallback_ratio = 0.90
        try:
            min_sales = int(os.getenv('MC_REQUEST_MIN_SALES', '5') or 5)
        except Exception:
            min_sales = 5

        # ── 2. Sales history median (USD) — 30d window, fallback 7d ─────────
        sales_median = None
        sales_count = 0
        try:
            hist_data = self.get_sales_history(
                skin_name,
                target_wear=target_wear,
                require_stattrak=bool(require_stattrak),
                allow_refresh=True,
            )
            if isinstance(hist_data, dict):
                raw_hist = hist_data.get('history') or []
                now_ts = time.time()
                month_ago = now_ts - 30 * 24 * 3600
                week_ago  = now_ts - 7  * 24 * 3600

                prices_30d: List[float] = []
                prices_7d:  List[float] = []
                for entry in raw_hist:
                    try:
                        if isinstance(entry, (list, tuple)) and len(entry) >= 3:
                            ts = float(entry[0])
                            p  = float(entry[2])
                            if p > 0:
                                if ts >= month_ago:
                                    prices_30d.append(p)
                                if ts >= week_ago:
                                    prices_7d.append(p)
                    except Exception:
                        continue

                # Prefer 30d if enough sales, else 7d
                if len(prices_30d) >= min_sales:
                    prices_30d.sort()
                    sales_median = float(prices_30d[len(prices_30d) // 2])
                    sales_count  = len(prices_30d)
                elif len(prices_7d) >= min_sales:
                    prices_7d.sort()
                    sales_median = float(prices_7d[len(prices_7d) // 2])
                    sales_count  = len(prices_7d)
        except Exception:
            pass

        # ── 4. Compute candidate ─────────────────────────────────────────────
        if sales_median is not None and sales_count >= min_sales:
            candidate = round(sales_median * median_ratio, 2)
        else:
            # No reliable history — fall back to ask ratio
            candidate = round(best_ask * fallback_ratio, 2)
            sales_median = None  # mark as unused

        # Hard ceiling: always cheaper than ask
        ceiling = round(best_ask * max_vs_ask, 2)
        if candidate > ceiling:
            candidate = ceiling

        # Hard floor: don't lowball (seller won't accept)
        floor = round(best_ask * min_vs_ask, 2)
        if candidate < floor:
            candidate = floor

        if candidate <= 0:
            return None

        suggested = round(candidate, 2)
        savings = round(best_ask - suggested, 2)
        savings_pct = round(savings / best_ask * 100.0, 1) if best_ask > 0 else 0.0

        return {
            'suggested_price': suggested,
            'best_ask': best_ask,
            'best_bid': best_bid,
            'sales_median': sales_median,
            'sales_count': sales_count,
            'savings_vs_ask': savings,
            'savings_pct': savings_pct,
        }

    def suggest_sell_price(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        require_stattrak: bool = False,
    ) -> Optional[Dict]:
        """
        Suggest an instant-sell price on market.csgo — the price at which
        a buyer will purchase the item immediately (best bid / buy order).
        Only returned if bid is at least MC_SELL_MIN_BID_VS_ASK % of ask (default 70%).

        Returns dict:
          'instant_sell'   — float, best current buy order (what you get right now)
          'best_ask'       — float, cheapest listing (for reference)
          'fee_pct'        — float, market fee %
          'net_receive'    — float, instant_sell * (1 - fee)
          'vs_ask_pct'     — float, instant_sell as % of best_ask
        Returns None if no buy orders exist or bid is too low.
        """
        book = self.get_order_book(skin_name, target_wear=target_wear, require_stattrak=require_stattrak)
        if not book or book.get('best_bid') is None:
            return None

        best_bid = float(book['best_bid'])
        best_ask = float(book['best_ask'])

        try:
            fee = float(os.getenv('MARKET_SELL_FEE', '0.07') or 0.07)
        except Exception:
            fee = 0.07
        try:
            min_bid_ratio = float(os.getenv('MC_SELL_MIN_BID_VS_ASK', '0.70') or 0.70)
        except Exception:
            min_bid_ratio = 0.70

        vs_ask_pct = round(best_bid / best_ask * 100.0, 1) if best_ask > 0 else 0.0

        # Skip stale/junk bids that are too far below ask
        if best_ask > 0 and best_bid < best_ask * min_bid_ratio:
            return None

        net = round(best_bid * (1.0 - fee), 2)

        return {
            'instant_sell': round(best_bid, 2),
            'best_ask': round(best_ask, 2),
            'fee_pct': round(fee * 100.0, 1),
            'net_receive': net,
            'vs_ask_pct': vs_ask_pct,
        }

    def _load_mock_prices(self) -> bool:
        """Загрузка моковых цен для тестирования"""
        logger.warning("Используем моковые цены для тестирования")
        
        mock_data = {
            'desert eagle blaze': [(1.50, 0.06, 'Factory New', False)],
            'ak-47 redline': [(3.20, 0.25, 'Field-Tested', False)],
            'awp asiimov': [(4.50, 0.15, 'Minimal Wear', False)],
            'm4a4 bullet rain': [(2.80, 0.30, 'Field-Tested', False)],
            'glock-18 water elemental': [(1.20, 0.20, 'Field-Tested', False)],
            'usp-s orion': [(2.50, 0.10, 'Factory New', False)],
            'm4a1-s hyper beast': [(3.00, 0.18, 'Minimal Wear', False)],
            'nova dark sigil': [(0.80, 0.35, 'Field-Tested', False)],
            'ssg 08 dezastre': [(0.80, 0.40, 'Field-Tested', False)],
            'p2000 sure grip': [(0.82, 0.28, 'Field-Tested', False)],
            'mp5-sd focus': [(0.82, 0.32, 'Field-Tested', False)],
            'dual berettas hideout': [(0.85, 0.22, 'Field-Tested', False)],
            'mag-7 insomnia': [(0.85, 0.38, 'Field-Tested', False)],
            'sawed-off spirit board': [(0.88, 0.45, 'Well-Worn', False)],
            'ump-45 roadblock': [(0.93, 0.26, 'Field-Tested', False)],
            'pp-bizon runic': [(1.05, 0.33, 'Field-Tested', False)],
            'mp5-sd liquidation': [(1.10, 0.29, 'Field-Tested', False)],
            'mp5-sd necro jr': [(1.11, 0.31, 'Field-Tested', False)],
        }
        
        self._prices_cache = mock_data
        self._last_update_time = time.time()
        self._total_lots_analyzed = len(mock_data)
        
        return True


class CSFloatClient:
    def __init__(self):
        load_dotenv()

        self.api_key = str(os.getenv('CSFLOAT_API_KEY', '') or '').strip()
        self.base_url = str(os.getenv('CSFLOAT_BASE_URL', 'https://csfloat.com/api/v1') or '').strip()
        self.enabled = str(os.getenv('CSFLOAT_ENABLED', '1' if self.api_key else '0') or '').strip() not in {'0', 'false', 'False', 'no', 'NO'}

        try:
            self.http_timeout_seconds = float(os.getenv('CSFLOAT_HTTP_TIMEOUT', '20') or 20)
        except Exception:
            self.http_timeout_seconds = 20.0
        try:
            self.http_retries = int(os.getenv('CSFLOAT_HTTP_RETRIES', '2') or 2)
        except Exception:
            self.http_retries = 2
        try:
            self.http_backoff_base_seconds = float(os.getenv('CSFLOAT_HTTP_BACKOFF_BASE', '0.5') or 0.5)
        except Exception:
            self.http_backoff_base_seconds = 0.5

        try:
            self.request_min_interval_seconds = float(os.getenv('CSFLOAT_REQUEST_MIN_INTERVAL', '0.4') or 0.4)
        except Exception:
            self.request_min_interval_seconds = 0.4
        if self.request_min_interval_seconds < 0:
            self.request_min_interval_seconds = 0.0

        try:
            self.rate_limit_cooldown_default_seconds = float(os.getenv('CSFLOAT_RATE_LIMIT_COOLDOWN_DEFAULT', '10') or 10)
        except Exception:
            self.rate_limit_cooldown_default_seconds = 10.0
        if self.rate_limit_cooldown_default_seconds < 0:
            self.rate_limit_cooldown_default_seconds = 0.0

        try:
            self.max_pages = int(os.getenv('CSFLOAT_MAX_PAGES', '1') or 1)
        except Exception:
            self.max_pages = 1
        if self.max_pages < 1:
            self.max_pages = 1

        try:
            self.cache_ttl_seconds = float(os.getenv('CSFLOAT_CACHE_TTL', '60') or 60)
        except Exception:
            self.cache_ttl_seconds = 60.0
        if self.cache_ttl_seconds < 0:
            self.cache_ttl_seconds = 0.0

        self._session = requests.Session()
        self._session.headers.update({'User-Agent': 'CS2-Contract-Analyzer/1.0'})
        # Explicitly bypass any system/env proxy (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY)
        # CSFloat must connect directly — proxy IPs get rate-limited quickly
        self._session.trust_env = False

        self._cache_lock = threading.RLock()
        self._listings_cache: Dict[Tuple[str, str, int], Tuple[float, List[Dict]]] = {}

        self._stats_lock = threading.RLock()
        self._stats_enabled = str(os.getenv('CSFLOAT_STATS', '0') or '').strip() in {'1', 'true', 'True', 'yes', 'YES'}
        self._stat_cache_hit = 0
        self._stat_cache_miss = 0
        self._stat_429 = 0
        self._stat_request_json = 0
        self._stat_unique_mhn: set = set()

        self._rate_limit_lock = threading.RLock()
        self._rate_limit_until_ts = 0.0
        self._last_request_ts = 0.0

        try:
            self.fail_fast_on_429 = str(os.getenv('CSFLOAT_FAIL_FAST_ON_429', '1') or '').strip() not in {'0', 'false', 'False', 'no', 'NO'}
        except Exception:
            self.fail_fast_on_429 = True

        # --- Session-level 429 protection ---
        # Auto-disables CSFloat for the current session after too many consecutive 429s
        # or after exceeding the per-session request budget.
        try:
            self._consecutive_429_limit = int(os.getenv('CSFLOAT_MAX_CONSECUTIVE_429', '5') or 5)
        except Exception:
            self._consecutive_429_limit = 5
        if self._consecutive_429_limit < 1:
            self._consecutive_429_limit = 1

        try:
            self._session_request_limit = int(os.getenv('CSFLOAT_SESSION_REQUEST_LIMIT', '150') or 150)
        except Exception:
            self._session_request_limit = 150
        if self._session_request_limit < 0:
            self._session_request_limit = 0

        self._consecutive_429s: int = 0
        self._session_requests_made: int = 0
        # When True, _request_json returns None immediately without network calls.
        self._session_disabled: bool = False

    def _rate_limit(self) -> None:
        if float(self.request_min_interval_seconds) <= 0 and float(self.rate_limit_cooldown_default_seconds) <= 0:
            return

        while True:
            with self._rate_limit_lock:
                now = time.time()
                sleep_s = 0.0

                if float(self._rate_limit_until_ts) > now:
                    sleep_s = max(sleep_s, float(self._rate_limit_until_ts) - now)

                if float(self.request_min_interval_seconds) > 0 and float(self._last_request_ts) > 0:
                    delta = now - float(self._last_request_ts)
                    if delta < float(self.request_min_interval_seconds):
                        sleep_s = max(sleep_s, float(self.request_min_interval_seconds) - delta)

                if sleep_s <= 1e-6:
                    self._last_request_ts = time.time()
                    return

            try:
                time.sleep(min(5.0, float(sleep_s)))
            except Exception:
                return

    def _apply_429_cooldown(self, retry_after_seconds: Optional[float], consecutive: int = 0) -> float:
        """Compute and apply rate-limit cooldown.  Returns the actual sleep duration in seconds."""
        try:
            ra = float(retry_after_seconds) if retry_after_seconds is not None else 0.0
        except Exception:
            ra = 0.0

        if ra > 0:
            # Server told us exactly how long to wait — respect it exactly.
            cooldown = ra
        else:
            # Exponential backoff: base * 2^consecutive_429s, capped at 5 minutes.
            base = float(self.rate_limit_cooldown_default_seconds)
            cooldown = min(base * (2 ** max(0, int(consecutive))), 300.0)

        if cooldown <= 0:
            return 0.0
        with self._rate_limit_lock:
            self._rate_limit_until_ts = max(float(self._rate_limit_until_ts), time.time() + float(cooldown))
        return cooldown

    def get_stats_snapshot(self) -> Dict[str, Any]:
        with self._stats_lock:
            return {
                'cache_hit': int(self._stat_cache_hit),
                'cache_miss': int(self._stat_cache_miss),
                'http_429': int(self._stat_429),
                'request_json': int(self._stat_request_json),
                'unique_market_hash_name': int(len(self._stat_unique_mhn)),
                'consecutive_429s': int(self._consecutive_429s),
                'session_requests_made': int(self._session_requests_made),
                'session_disabled': bool(self._session_disabled),
            }

    def reset_session_limits(self) -> None:
        """Re-enable CSFloat after a manual intervention (e.g. next refresh cycle)."""
        with self._stats_lock:
            self._consecutive_429s = 0
            self._session_requests_made = 0
            self._session_disabled = False
        logger.info('CSFloat session limits reset: client re-enabled')

    def _build_market_hash_name(self, skin_name: str, *, target_wear: Optional[str], require_stattrak: bool) -> Optional[str]:
        nm = str(skin_name or '').strip()
        w = str(target_wear or '').strip()
        if not nm or not w:
            return None
        prefix = 'StatTrak™ ' if bool(require_stattrak) else ''
        return f"{prefix}{nm} ({w})"

    def _request_json(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        if not self.enabled or not self.api_key:
            return None

        # Session-level guard: auto-disabled after too many consecutive 429s
        # or after exhausting the per-session request budget.
        with self._stats_lock:
            if self._session_disabled:
                return None

        url = str(self.base_url).rstrip('/') + '/' + str(path).lstrip('/')

        last_exc: Optional[Exception] = None
        for attempt in range(max(1, int(self.http_retries))):
            try:
                with self._stats_lock:
                    self._stat_request_json += 1
                self._rate_limit()
                resp = self._session.get(
                    url,
                    params=params,
                    timeout=float(self.http_timeout_seconds),
                    headers={'Authorization': self.api_key},
                )
                if resp.status_code == 429:
                    with self._stats_lock:
                        self._stat_429 += 1
                        self._consecutive_429s += 1
                        consecutive = int(self._consecutive_429s)
                        limit_reached = consecutive >= int(self._consecutive_429_limit)

                    try:
                        ra = float(resp.headers.get('Retry-After') or 0)
                    except Exception:
                        ra = 0.0

                    cooldown = self._apply_429_cooldown(ra, consecutive=consecutive - 1)

                    logger.warning(
                        'CSFloat 429 #%d (consecutive), cooldown=%.0fs, session_total=%d/%d, limit_reached=%s',
                        consecutive,
                        cooldown,
                        self._session_requests_made,
                        self._session_request_limit,
                        limit_reached,
                    )

                    if limit_reached:
                        logger.warning(
                            'CSFloat auto-disabled: %d consecutive 429s reached limit=%d. '
                            'Call reset_session_limits() to re-enable.',
                            consecutive,
                            self._consecutive_429_limit,
                        )
                        with self._stats_lock:
                            self._session_disabled = True
                        return None

                    if bool(self.fail_fast_on_429):
                        return None

                    continue

                resp.raise_for_status()

                # Successful response — reset consecutive counter, track budget.
                with self._stats_lock:
                    self._consecutive_429s = 0
                    self._session_requests_made += 1
                    budget_exhausted = (
                        int(self._session_request_limit) > 0
                        and int(self._session_requests_made) >= int(self._session_request_limit)
                    )
                if budget_exhausted:
                    logger.warning(
                        'CSFloat session request budget exhausted (%d requests). Auto-disabled for this session.',
                        self._session_requests_made,
                    )
                    with self._stats_lock:
                        self._session_disabled = True

                try:
                    return resp.json()
                except Exception as e:
                    last_exc = e
                    return None
            except Exception as e:
                last_exc = e
                if attempt + 1 < max(1, int(self.http_retries)):
                    try:
                        time.sleep(max(0.0, float(self.http_backoff_base_seconds)) * (2 ** attempt))
                    except Exception:
                        pass
                continue

        if last_exc is not None:
            try:
                logger.info('CSFloat request failed: path=%s err=%s', str(path), f"{type(last_exc).__name__}: {last_exc}")
            except Exception:
                pass
        return None

    def _get_listings_cached(self, *, market_hash_name: str, listing_type: str, limit: int) -> List[Dict]:
        key = (str(market_hash_name), str(listing_type), int(limit))
        now = time.time()
        with self._cache_lock:
            cached = self._listings_cache.get(key)
        if cached is not None:
            ts, data = cached
            if float(self.cache_ttl_seconds) <= 0 or (now - float(ts)) < float(self.cache_ttl_seconds):
                with self._stats_lock:
                    self._stat_cache_hit += 1
                    if self._stats_enabled:
                        self._stat_unique_mhn.add(str(market_hash_name))
                return list(data)

        with self._stats_lock:
            self._stat_cache_miss += 1
            if self._stats_enabled:
                self._stat_unique_mhn.add(str(market_hash_name))

        params: Dict[str, Any] = {
            'type': str(listing_type),
            'market_hash_name': str(market_hash_name),
            'limit': int(limit),
        }

        all_items: List[Dict] = []
        cursor = None
        pages = max(1, int(self.max_pages))
        for _page_i in range(pages):
            if cursor:
                params['cursor'] = str(cursor)
            obj = self._request_json('/listings', params=params)
            if not isinstance(obj, dict):
                break
            data = obj.get('data')
            if not isinstance(data, list) or not data:
                break
            all_items.extend([x for x in data if isinstance(x, dict)])
            cursor = obj.get('cursor')
            if not cursor:
                break
            if int(limit) > 0 and len(all_items) >= int(limit):
                break

        with self._cache_lock:
            self._listings_cache[key] = (time.time(), list(all_items))

        if self._stats_enabled:
            try:
                snap = self.get_stats_snapshot()
                logger.info(
                    'CSFloat stats: cache_hit=%s cache_miss=%s http_429=%s request_json=%s unique_mhn=%s',
                    snap.get('cache_hit'),
                    snap.get('cache_miss'),
                    snap.get('http_429'),
                    snap.get('request_json'),
                    snap.get('unique_market_hash_name'),
                )
            except Exception:
                pass
        return list(all_items)

    def get_listings(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        max_float: Optional[float] = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        limit: int = 50,
    ) -> List[Tuple[float, Optional[float], str]]:
        if not self.enabled or not self.api_key:
            return []
        if bool(exclude_stattrak) and bool(require_stattrak):
            return []

        mhn = self._build_market_hash_name(skin_name, target_wear=target_wear, require_stattrak=bool(require_stattrak))
        if not mhn:
            return []

        raw = self._get_listings_cached(market_hash_name=mhn, listing_type='buy_now', limit=max(1, int(limit)))
        out: List[Tuple[float, Optional[float], str]] = []
        mf = float(max_float) if max_float is not None else None
        for x in raw:
            item = x.get('item') if isinstance(x, dict) else None
            if not isinstance(item, dict):
                continue
            if bool(exclude_stattrak) and bool(item.get('is_stattrak')):
                continue
            if bool(require_stattrak) and (not bool(item.get('is_stattrak'))):
                continue

            try:
                cents = int(x.get('price'))
            except Exception:
                continue
            if cents <= 0:
                continue
            price = float(cents) / 100.0

            fval = item.get('float_value')
            try:
                fval2 = float(fval) if fval is not None else None
            except Exception:
                fval2 = None
            if mf is not None and fval2 is not None and float(fval2) > float(mf) + 1e-12:
                continue

            wear_name = str(item.get('wear_name') or target_wear or '').strip()
            out.append((price, fval2, wear_name))

        out.sort(key=lambda t: float(t[0]))
        return out

    def get_price(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        max_float: Optional[float] = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        limit: int = 50,
    ) -> Optional[float]:
        lots = self.get_listings(
            skin_name,
            target_wear=target_wear,
            max_float=max_float,
            exclude_stattrak=exclude_stattrak,
            require_stattrak=require_stattrak,
            limit=limit,
        )
        if not lots:
            return None
        try:
            return float(lots[0][0])
        except Exception:
            return None


class DMarketClient:
    """Client for DMarket Trading API with Ed25519 request signing."""

    BASE_URL = 'https://api.dmarket.com'
    GAME_ID = 'a8db'  # CS2

    def __init__(self):
        self.public_key = str(os.getenv('DMARKET_PUBLIC_KEY', '') or '').strip()
        self.secret_key = str(os.getenv('DMARKET_SECRET_KEY', '') or '').strip()
        self.enabled = bool(
            self.public_key and self.secret_key
            and str(os.getenv('DMARKET_ENABLED', '1' if self.public_key else '0') or '').strip()
            not in {'0', 'false', 'no', 'off'}
        )

        try:
            self.http_timeout = float(os.getenv('DMARKET_HTTP_TIMEOUT', '20') or 20)
        except Exception:
            self.http_timeout = 20.0
        try:
            self.request_min_interval = float(os.getenv('DMARKET_REQUEST_MIN_INTERVAL', '0.1') or 0.1)
        except Exception:
            self.request_min_interval = 0.1
        try:
            self.cache_ttl = float(os.getenv('DMARKET_CACHE_TTL', '60') or 60)
        except Exception:
            self.cache_ttl = 60.0

        self._session = requests.Session()
        self._session.headers.update({'User-Agent': 'CS2-Contract-Analyzer/1.0'})
        self._last_request_ts: float = 0.0
        self._rate_lock = threading.RLock()

        self._cache_lock = threading.RLock()
        self._listings_cache: dict = {}   # key -> (ts, data)
        self._prices_cache: dict = {}     # title -> (ts, price_usd)
        self._last_sales_cache: dict = {} # (title, limit) -> (ts, prices)

        self._signing_available = False
        if self.enabled:
            try:
                import nacl.signing
                import nacl.encoding
                secret_bytes = bytes.fromhex(self.secret_key)
                # DMarket returns 64-byte key (seed + public) — use only first 32 bytes as seed
                seed = secret_bytes[:32]
                self._signing_key = nacl.signing.SigningKey(seed)
                self._signing_available = True
            except ImportError:
                logger.warning('DMarket: PyNaCl not installed. Run: pip install PyNaCl')
            except Exception as e:
                logger.warning('DMarket: failed to init signing key: %s', e)

    def _rate_limit(self) -> None:
        with self._rate_lock:
            now = time.time()
            delta = now - self._last_request_ts
            if delta < self.request_min_interval:
                time.sleep(self.request_min_interval - delta)
            self._last_request_ts = time.time()

    def _sign_request(self, method: str, path: str, body: str = '') -> dict:
        """Build signed headers for a DMarket API request."""
        if not self._signing_available:
            return {}
        try:
            import nacl.signing
            import nacl.encoding
            ts = str(int(time.time()))
            unsigned = f'{method}{path}{body}{ts}'
            signed = self._signing_key.sign(unsigned.encode('utf-8'))
            signature = signed.signature.hex()
            return {
                'X-Api-Key': self.public_key,
                'X-Sign-Date': ts,
                'X-Request-Sign': f'dmar ed25519 {signature}',
                'Content-Type': 'application/json',
            }
        except Exception as e:
            logger.debug('DMarket sign error: %s', e)
            return {}

    def _get(self, path: str, params: dict = None, timeout: float = None) -> Optional[dict]:
        if not self.enabled or not self._signing_available:
            return None
        import urllib.parse
        query = ''
        if params:
            sorted_params = sorted((k, v) for k, v in params.items() if v is not None)
            parts = []
            for k, v in sorted_params:
                # Preserve [], =, spaces as %20 — match what DMarket expects in signature
                enc_k = urllib.parse.quote(str(k), safe='[]')
                enc_v = urllib.parse.quote(str(v), safe='[]=')
                parts.append(f'{enc_k}={enc_v}')
            query = '?' + '&'.join(parts)
        full_path = path + query
        headers = self._sign_request('GET', full_path)
        if not headers:
            return None
        self._rate_limit()
        req_timeout = timeout if timeout is not None else self.http_timeout
        try:
            resp = self._session.get(
                self.BASE_URL + full_path,
                headers=headers,
                timeout=req_timeout,
            )
            if resp.status_code == 429:
                logger.warning('DMarket: rate limited (429)')
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug('DMarket GET %s failed: %s', path, e)
            return None

    def _post(self, path: str, body: dict) -> Optional[dict]:
        if not self.enabled or not self._signing_available:
            return None
        import json as _json
        body_str = _json.dumps(body, separators=(',', ':'))
        headers = self._sign_request('POST', path, body_str)
        if not headers:
            return None
        self._rate_limit()
        try:
            resp = self._session.post(
                self.BASE_URL + path,
                headers=headers,
                data=body_str,
                timeout=self.http_timeout,
            )
            if resp.status_code == 429:
                logger.warning('DMarket: rate limited (429)')
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug('DMarket POST %s failed: %s', path, e)
            return None

    def get_aggregated_prices(self, titles: List[str]) -> Dict[str, float]:
        """
        Get best sell prices for a list of item titles.
        Returns {title: price_usd} using offerBestPrice.
        Prices are in cents in the API response — converted to USD here.
        """
        if not titles:
            return {}

        # Check cache
        now = time.time()
        result: Dict[str, float] = {}
        missing = []
        with self._cache_lock:
            for t in titles:
                cached = self._prices_cache.get(t)
                if cached and (now - cached[0]) < self.cache_ttl:
                    result[t] = cached[1]
                else:
                    missing.append(t)

        if not missing:
            return result

        # Batch in chunks of 100
        chunk_size = 100
        for i in range(0, len(missing), chunk_size):
            chunk = missing[i:i + chunk_size]
            data = self._post('/marketplace-api/v1/aggregated-prices', {
                'filter': {'game': self.GAME_ID, 'titles': chunk},
                'limit': str(len(chunk)),
            })
            if not data:
                continue
            for item in data.get('aggregatedPrices') or []:
                title = str(item.get('title') or '')
                offer_price = item.get('offerBestPrice') or {}
                # API returns Amount (capitalized) in cents
                usd_cents = offer_price.get('Amount') or offer_price.get('USD')
                if usd_cents is not None:
                    try:
                        price_usd = float(usd_cents) / 100.0
                        result[title] = price_usd
                        with self._cache_lock:
                            self._prices_cache[title] = (time.time(), price_usd)
                    except Exception:
                        pass

        return result

    def get_order_book(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        require_stattrak: bool = False,
    ) -> Optional[Dict]:
        """
        Fetch best ask (offerBestPrice) and best bid (orderBestPrice) from DMarket
        aggregated-prices endpoint.
        Returns {'best_ask': float, 'best_bid': float|None, 'currency': 'USD'} or None.
        Prices in API are in cents — converted to USD here.
        """
        if not self.enabled or not self._signing_available:
            return None

        st_prefix = 'StatTrak\u2122 ' if bool(require_stattrak) else ''
        if target_wear:
            title = f'{st_prefix}{skin_name} ({target_wear})'
        else:
            title = f'{st_prefix}{skin_name}'

        data = self._post('/marketplace-api/v1/aggregated-prices', {
            'filter': {'game': self.GAME_ID, 'titles': [title]},
            'limit': '1',
        })
        if not data:
            return None

        for item in data.get('aggregatedPrices') or []:
            if str(item.get('title') or '') != title:
                continue
            try:
                offer = item.get('offerBestPrice') or {}
                order = item.get('orderBestPrice') or {}
                ask_cents = offer.get('Amount') or offer.get('USD')
                bid_cents = order.get('Amount') or order.get('USD')
                best_ask = float(ask_cents) / 100.0 if ask_cents is not None else None
                best_bid = float(bid_cents) / 100.0 if bid_cents is not None else None
                if best_ask is None:
                    return None
                # Sanity: ignore bids < 1% of ask
                if best_bid is not None and best_bid < best_ask * 0.01:
                    best_bid = None
                return {'best_ask': best_ask, 'best_bid': best_bid, 'currency': 'USD'}
            except Exception:
                return None

        return None

    def suggest_request_price(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        require_stattrak: bool = False,
    ) -> Optional[Dict]:
        """
        Suggest an optimal buy-order (target order) price on DMarket.
        Uses the same median-based strategy as MarketCSGOClient.suggest_request_price:
          - Anchor: median of last N sales from get_last_sales
          - suggested = median * MC_REQUEST_MEDIAN_RATIO (default 0.97)
          - Hard ceiling: best_ask * MC_REQUEST_MAX_VS_ASK (default 0.95)
          - Hard floor:   best_ask * MC_REQUEST_MIN_VS_ASK (default 0.70)
          - Fallback (no history): best_ask * MC_REQUEST_FALLBACK_RATIO (default 0.90)

        Returns dict with suggested_price, best_ask, best_bid, sales_median,
        savings_vs_ask, savings_pct — or None if unavailable.
        """
        book = self.get_order_book(skin_name, target_wear=target_wear, require_stattrak=require_stattrak)
        if not book:
            return None
        best_ask = float(book['best_ask'])
        best_bid = book.get('best_bid')

        # Sales history median from last_sales
        sales_median = None
        sales_count = 0
        try:
            min_sales = int(os.getenv('MC_REQUEST_MIN_SALES', '5') or 5)
        except Exception:
            min_sales = 5
        try:
            # get_last_sales uses title directly — prepend StatTrak™ if needed
            st_prefix = 'StatTrak\u2122 ' if bool(require_stattrak) else ''
            sales_skin_name = f'{st_prefix}{skin_name}'
            sales = self.get_last_sales(sales_skin_name, target_wear=target_wear, limit=50)
            if sales and len(sales) >= min_sales:
                s = sorted(sales)
                sales_median = float(s[len(s) // 2])
                sales_count = len(s)
        except Exception:
            pass

        # Config (shared with MC)
        try:
            median_ratio = float(os.getenv('MC_REQUEST_MEDIAN_RATIO', '0.97') or 0.97)
        except Exception:
            median_ratio = 0.97
        try:
            max_vs_ask = float(os.getenv('MC_REQUEST_MAX_VS_ASK', '0.95') or 0.95)
        except Exception:
            max_vs_ask = 0.95
        try:
            min_vs_ask = float(os.getenv('MC_REQUEST_MIN_VS_ASK', '0.70') or 0.70)
        except Exception:
            min_vs_ask = 0.70
        try:
            fallback_ratio = float(os.getenv('MC_REQUEST_FALLBACK_RATIO', '0.90') or 0.90)
        except Exception:
            fallback_ratio = 0.90

        if sales_median is not None and sales_count >= min_sales:
            candidate = round(sales_median * median_ratio, 2)
        else:
            candidate = round(best_ask * fallback_ratio, 2)
            sales_median = None

        # Hard ceiling
        ceiling = round(best_ask * max_vs_ask, 2)
        if candidate > ceiling:
            candidate = ceiling

        # Hard floor
        floor = round(best_ask * min_vs_ask, 2)
        if candidate < floor:
            candidate = floor

        if candidate <= 0:
            return None

        suggested = round(candidate, 2)
        savings = round(best_ask - suggested, 2)
        savings_pct = round(savings / best_ask * 100.0, 1) if best_ask > 0 else 0.0

        return {
            'suggested_price': suggested,
            'best_ask': best_ask,
            'best_bid': best_bid,
            'sales_median': sales_median,
            'sales_count': sales_count,
            'savings_vs_ask': savings,
            'savings_pct': savings_pct,
        }

    def suggest_sell_price(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        require_stattrak: bool = False,
    ) -> Optional[Dict]:
        """
        Suggest an instant-sell price on DMarket — the best target order
        (orderBestPrice) that will fill immediately.
        Only returned if bid >= DM_SELL_MIN_BID_VS_ASK % of ask (default 70%).

        Returns dict:
          'instant_sell'  — float, best current buy order (USD)
          'best_ask'      — float, cheapest listing (for reference)
          'fee_pct'       — float, DMarket fee %
          'net_receive'   — float, instant_sell * (1 - fee)
          'vs_ask_pct'    — float, instant_sell as % of best_ask
        Returns None if no buy orders exist or bid is too low.
        """
        book = self.get_order_book(skin_name, target_wear=target_wear, require_stattrak=require_stattrak)
        if not book or book.get('best_bid') is None:
            return None

        best_bid = float(book['best_bid'])
        best_ask = float(book['best_ask'])

        try:
            fee = float(os.getenv('DMARKET_SELL_FEE', '0.05') or 0.05)
        except Exception:
            fee = 0.05
        try:
            min_bid_ratio = float(os.getenv('MC_SELL_MIN_BID_VS_ASK', '0.70') or 0.70)
        except Exception:
            min_bid_ratio = 0.70

        vs_ask_pct = round(best_bid / best_ask * 100.0, 1) if best_ask > 0 else 0.0

        # Skip stale/junk bids
        if best_ask > 0 and best_bid < best_ask * min_bid_ratio:
            return None

        net = round(best_bid * (1.0 - fee), 2)

        return {
            'instant_sell': round(best_bid, 2),
            'best_ask': round(best_ask, 2),
            'fee_pct': round(fee * 100.0, 1),
            'net_receive': net,
            'vs_ask_pct': vs_ask_pct,
        }

    def get_listings(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        max_float: Optional[float] = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        limit: int = 50,
    ) -> List[Tuple[float, Optional[float], str]]:
        """
        Get listings for a skin from DMarket with real float values.
        Returns list of (price_usd, float_value, wear).
        """
        if not self.enabled or not self._signing_available:
            return []

        # Build title with wear suffix for DMarket
        wear_map = {
            'Factory New': 'Factory New',
            'Minimal Wear': 'Minimal Wear',
            'Field-Tested': 'Field-Tested',
            'Well-Worn': 'Well-Worn',
            'Battle-Scarred': 'Battle-Scarred',
        }
        st_prefix = 'StatTrak™ ' if require_stattrak else ''
        if target_wear and target_wear in wear_map:
            title = f'{st_prefix}{skin_name} ({wear_map[target_wear]})'
        else:
            title = f'{st_prefix}{skin_name}'

        # Check cache
        cache_key = (title, max_float, exclude_stattrak, require_stattrak, limit)
        now = time.time()
        with self._cache_lock:
            cached = self._listings_cache.get(cache_key)
            if cached and (now - cached[0]) < self.cache_ttl:
                return list(cached[1])

        params = {
            'Title': title,
            'Limit': str(min(limit, 100)),
        }
        data = self._get('/exchange/v1/offers-by-title', params)
        if not data:
            return []

        lots: List[Tuple[float, Optional[float], str]] = []
        for obj in data.get('objects') or []:
            try:
                obj_title = str(obj.get('title') or '')
                # Skip souvenir items — cannot be used in trade-up contracts
                if 'souvenir' in obj_title.lower():
                    continue

                price_obj = obj.get('price') or {}
                usd_cents = price_obj.get('USD')
                if usd_cents is None:
                    continue
                price_usd = float(usd_cents) / 100.0
                if price_usd <= 0:
                    continue

                extra = obj.get('extra') or {}
                float_val = extra.get('floatValue')
                try:
                    float_val = float(float_val) if float_val is not None else None
                except Exception:
                    float_val = None

                wear = str(extra.get('exterior') or '').replace('-', ' ').title()
                # Normalize wear name
                wear_norm = {
                    'Factory New': 'Factory New',
                    'Minimal Wear': 'Minimal Wear',
                    'Field Tested': 'Field-Tested',
                    'Well Worn': 'Well-Worn',
                    'Battle Scarred': 'Battle-Scarred',
                }.get(wear, wear)

                # Float filter
                if max_float is not None and float_val is not None:
                    if float_val > float(max_float) + 1e-9:
                        continue

                # StatTrak filter
                is_st = 'stattrak' in str(obj.get('title') or '').lower()
                if exclude_stattrak and is_st:
                    continue
                if require_stattrak and not is_st:
                    continue

                lots.append((price_usd, float_val, wear_norm))
            except Exception:
                continue

        lots.sort(key=lambda x: (x[0], 1.0 if x[1] is None else x[1]))
        lots = lots[:limit]

        with self._cache_lock:
            self._listings_cache[cache_key] = (time.time(), list(lots))

        return lots

    def get_price(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        max_float: Optional[float] = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        limit: int = 10,
    ) -> Optional[float]:
        lots = self.get_listings(
            skin_name,
            target_wear=target_wear,
            max_float=max_float,
            exclude_stattrak=exclude_stattrak,
            require_stattrak=require_stattrak,
            limit=limit,
        )
        return float(lots[0][0]) if lots else None

    def get_last_sales(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        limit: int = 20,
    ) -> List[float]:
        """Get recent sale prices for a skin. Returns list of USD prices.
        Results are cached for cache_ttl seconds to avoid hammering the API
        during refine (which calls this for every outcome of every contract).
        """
        # Include wear in title — DMarket matches by full item name
        wear_suffix = {
            'Factory New': 'Factory New',
            'Minimal Wear': 'Minimal Wear',
            'Field-Tested': 'Field-Tested',
            'Well-Worn': 'Well-Worn',
            'Battle-Scarred': 'Battle-Scarred',
        }
        title = skin_name
        if target_wear and target_wear in wear_suffix:
            title = f'{skin_name} ({wear_suffix[target_wear]})'

        cache_key = (title, min(limit, 20))
        now = time.time()
        with self._cache_lock:
            cached = self._last_sales_cache.get(cache_key)
            if cached is not None:
                ts, prices = cached
                if now - ts < self.cache_ttl:
                    return list(prices)

        params = {
            'gameId': self.GAME_ID,
            'title': title,
            'limit': str(min(limit, 20)),
        }

        data = self._get('/trade-aggregator/v1/last-sales', params)
        if not data:
            # Cache empty result too (briefly) to avoid hammering on missing skins
            with self._cache_lock:
                self._last_sales_cache[cache_key] = (now, [])
            return []

        prices = []
        for sale in data.get('sales') or []:
            try:
                # DMarket last-sales returns price as a string (USD cents or dollars)
                price_raw = sale.get('price')
                if price_raw is not None:
                    price_usd = float(price_raw)
                    # Values like 276.29 are already in USD (not cents)
                    if price_usd > 0:
                        prices.append(price_usd)
                else:
                    # Fallback: nested price object
                    price_obj = sale.get('price') or {}
                    if isinstance(price_obj, dict):
                        usd = price_obj.get('USD')
                        if usd is not None:
                            prices.append(float(usd) / 100.0)
            except Exception:
                continue

        with self._cache_lock:
            self._last_sales_cache[cache_key] = (now, list(prices))
        return prices

    def load_all_prices(self) -> Optional[Dict[str, List[Tuple[float, Optional[float], str, bool]]]]:
        """
        Load all CS2 market items from DMarket via paginated GET /exchange/v1/market/items.
        Returns cache dict compatible with MarketCSGOClient._prices_cache format:
          {normalized_skin_name: [(price, float_val, wear, is_stattrak), ...]}
        Rate limit: 10 RPS — at 100 items/page this takes ~10-20s for full catalog.
        Returns None on failure.
        """
        if not self.enabled or not self._signing_available:
            return None

        new_cache: Dict[str, List[Tuple[float, Optional[float], str, bool]]] = {}
        cursor = None
        page_size = 30  # stable page size based on testing
        total_loaded = 0
        max_pages = int(os.getenv('DMARKET_MAX_PAGES', '500') or 500)
        bulk_timeout = float(os.getenv('DMARKET_BULK_TIMEOUT', '30') or 30)
        max_retries = 3

        wear_norm_map = {
            'factory new': 'Factory New',
            'minimal wear': 'Minimal Wear',
            'field-tested': 'Field-Tested',
            'well-worn': 'Well-Worn',
            'battle-scarred': 'Battle-Scarred',
        }

        for page_i in range(max_pages):
            params: dict = {
                'gameId': self.GAME_ID,
                'currency': 'USD',
                'limit': str(page_size),
                'orderBy': 'price',
                'orderDir': 'asc',
            }
            if cursor:
                params['cursor'] = cursor

            data = self._get('/exchange/v1/market/items', params, timeout=bulk_timeout)
            if not data:
                # Retry with smaller page on timeout
                for retry in range(max_retries):
                    time.sleep(0.5)
                    data = self._get('/exchange/v1/market/items', params, timeout=bulk_timeout)
                    if data:
                        break
            if not data:
                logger.debug('DMarket load_all_prices: page %d returned no data after retries', page_i)
                break

            objects = data.get('objects') or []
            if not objects:
                break

            for obj in objects:
                try:
                    price_obj = obj.get('price') or {}
                    usd_cents = price_obj.get('USD')
                    if usd_cents is None:
                        # Try instantPrice as fallback
                        price_obj = obj.get('instantPrice') or {}
                        usd_cents = price_obj.get('USD')
                    if usd_cents is None:
                        continue
                    price_usd = float(usd_cents) / 100.0
                    if price_usd <= 0:
                        continue

                    title = str(obj.get('title') or '')
                    if ' | ' not in title:
                        continue
                    # Skip souvenir items — cannot be used in trade-up contracts
                    if 'souvenir' in title.lower():
                        continue

                    extra = obj.get('extra') or {}
                    float_val = extra.get('floatValue')
                    try:
                        float_val = float(float_val) if float_val is not None else None
                    except Exception:
                        float_val = None

                    exterior = str(extra.get('exterior') or '').lower()
                    # DMarket returns exterior as "battle-scarred" or "minimal wear" etc.
                    wear = wear_norm_map.get(exterior) or wear_norm_map.get(exterior.replace('-', ' '))
                    if not wear and float_val is not None:
                        # Determine wear from float
                        if float_val < 0.07:
                            wear = 'Factory New'
                        elif float_val < 0.15:
                            wear = 'Minimal Wear'
                        elif float_val < 0.38:
                            wear = 'Field-Tested'
                        elif float_val < 0.45:
                            wear = 'Well-Worn'
                        else:
                            wear = 'Battle-Scarred'

                    is_stattrak = 'stattrak' in title.lower()

                    # Normalize name (strip wear suffix and stattrak prefix)
                    norm_name = title.lower()
                    for w in wear_norm_map.values():
                        norm_name = norm_name.replace(f' ({w.lower()})', '')
                    norm_name = norm_name.replace('stattrak™ ', '').replace('stattrak™', '').strip()

                    if not norm_name:
                        continue

                    if norm_name not in new_cache:
                        new_cache[norm_name] = []
                    new_cache[norm_name].append((price_usd, float_val, wear, is_stattrak))
                    total_loaded += 1
                except Exception:
                    continue

            cursor = data.get('cursor')
            if not cursor:
                break

            # Rate limit: 10 RPS → 0.1s between requests (already handled by _rate_limit)

        if not new_cache:
            return None

        logger.info('DMarket: loaded %d items across %d unique skins', total_loaded, len(new_cache))
        return new_cache


class PriceManager:
    """Менеджер цен для работы с Full Export API"""
    
    def __init__(self):
        self.market_client = MarketCSGOClient()
        self.csfloat_client = CSFloatClient()
        self.dmarket_client = DMarketClient()
        # BID mode: {normalized_name: {wear: bid_price_usd}}
        # Populated by load_bid_prices(). Used by calculator in BID mode.
        self._bid_prices: Dict[str, Dict[str, float]] = {}
        self._bid_prices_lock = threading.RLock()
        self._bid_prices_ts: float = 0.0
        self._bid_prices_ttl: float = 300.0  # 5 min
    
    def initialize(self) -> bool:
        """Инициализация с быстрым стартом.

        Сначала грузим устаревший кэш с диска (если есть) — сервис сразу готов отвечать.
        _last_update_time намеренно НЕ обновляем, чтобы фоновый воркер
        bot_service.start_refresher() понял, что цены устарели, и подтянул свежие из API.
        Если диск-кэша нет вообще — блокирующий fetch из API (первый деплой).
        """
        mc = self.market_client
        # Быстрый путь: устаревший кэш
        stale = mc._load_disk_cache(allow_stale=True)
        if stale:
            with mc._prices_cache_lock:
                mc._prices_cache = stale
                mc._total_lots_analyzed = sum(len(v) for v in stale.values())
                # Не трогаем _last_update_time → _is_cache_valid() вернёт False
                # → при следующем refresh_prices() данные будут перезагружены из API.
            mc._reset_prefix_cache()
            import logging as _log
            _log.getLogger(__name__).info(
                'Fast startup: loaded stale disk cache (%d items). '
                'Fresh prices will be fetched by background refresher.',
                len(stale),
            )
            return True
        # Кэша нет совсем — блокирующий fetch (только при первом деплое)
        return mc.load_prices()

    def refresh_prices(self, force_refresh: bool = True) -> bool:
        """Refresh prices from market.csgo and optionally DMarket in parallel."""
        import threading as _threading

        market_ok = self.market_client.load_prices(force_refresh=force_refresh)

        # Load DMarket prices in background and merge into market cache
        if self.dmarket_client and bool(getattr(self.dmarket_client, 'enabled', False)):
            def _load_dmarket():
                try:
                    dm_cache = self.dmarket_client.load_all_prices()
                    if not dm_cache:
                        return
                    mc = self.market_client
                    # Only add DMarket lots for skins that have NO market.csgo data.
                    # This prevents DMarket's raw ask prices from inflating sell price estimates.
                    # DMarket is queried directly (with liquidity checks) when needed.
                    with mc._prices_cache_lock:
                        added = 0
                        for norm_name, lots in dm_cache.items():
                            if 'souvenir' in norm_name.lower():
                                continue
                            # Skip if market.csgo already has data for this skin
                            if norm_name in mc._prices_cache and mc._prices_cache[norm_name]:
                                continue
                            mc._prices_cache[norm_name] = list(lots)
                            added += 1
                    mc._reset_prefix_cache()
                    logger.info('DMarket: added %d skins with no market.csgo data', added)
                except Exception as e:
                    logger.debug('DMarket load_all_prices failed: %s', e)

            t = _threading.Thread(target=_load_dmarket, daemon=True)
            t.start()

        return market_ok
    
    def get_skin_price(
        self,
        skin_name: str,
        target_wear: str = None,
        max_float: float = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = False,
        allow_refresh: bool = True,
    ) -> Optional[float]:
        """
        Улучшенный поиск цены с маппингом на разные качества и фильтрацией StatTrak
        
        Args:
            skin_name: имя скина из базы данных (без качества)
            target_wear: целевое качество (опционально)
            max_float: максимальный допустимый float (опционально)
            exclude_stattrak: исключить StatTrak предметы (по умолчанию True)
            
        Returns:
            float: цена или None
        """
        prices = []

        # CSFloat — real float data, most accurate for float-filtered queries
        if self.csfloat_client and bool(getattr(self.csfloat_client, 'enabled', False)):
            try:
                val = self.csfloat_client.get_price(
                    skin_name,
                    target_wear=target_wear,
                    max_float=max_float,
                    exclude_stattrak=exclude_stattrak,
                    require_stattrak=require_stattrak,
                    limit=50,
                )
                if val is not None and float(val) > 0:
                    prices.append(float(val))
            except Exception:
                pass

        # DMarket — additional price source with real float data
        if self.dmarket_client and bool(getattr(self.dmarket_client, 'enabled', False)):
            try:
                val = self.dmarket_client.get_price(
                    skin_name,
                    target_wear=target_wear,
                    max_float=max_float,
                    exclude_stattrak=exclude_stattrak,
                    require_stattrak=require_stattrak,
                    limit=10,
                )
                if val is not None and float(val) > 0:
                    prices.append(float(val))
            except Exception:
                pass

        # Return cheapest price across sources
        if prices:
            return min(prices)

        return self.market_client.get_price(
            skin_name,
            target_wear,
            max_float,
            exclude_stattrak,
            require_stattrak=require_stattrak,
            strict_name_match=strict_name_match,
            allow_refresh=allow_refresh,
        )

    def get_price(
        self,
        skin_name: str,
        target_wear: str = None,
        max_float: float = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = False,
        allow_refresh: bool = True,
    ) -> Optional[float]:
        return self.get_skin_price(
            skin_name,
            target_wear=target_wear,
            max_float=max_float,
            exclude_stattrak=exclude_stattrak,
            require_stattrak=require_stattrak,
            strict_name_match=strict_name_match,
            allow_refresh=allow_refresh,
        )
    
    def get_skin_price_with_float(
        self,
        skin_name: str,
        target_wear: str = None,
        max_float: float = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = False,
        allow_refresh: bool = True,
    ) -> Optional[Tuple[float, float, str]]:
        """
        Улучшенный поиск цены с float информацией, маппингом и фильтрацией StatTrak
        
        Returns:
            Tuple[float, float, str]: (price, float_value, wear) или None
        """
        return self.market_client.get_price_with_float(
            skin_name,
            target_wear,
            max_float,
            exclude_stattrak,
            require_stattrak=require_stattrak,
            strict_name_match=strict_name_match,
            allow_refresh=allow_refresh,
        )

    def get_price_with_float(
        self,
        skin_name: str,
        target_wear: str = None,
        max_float: float = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = False,
        allow_refresh: bool = True,
    ) -> Optional[Tuple[float, float, str]]:
        return self.get_skin_price_with_float(
            skin_name,
            target_wear=target_wear,
            max_float=max_float,
            exclude_stattrak=exclude_stattrak,
            require_stattrak=require_stattrak,
            strict_name_match=strict_name_match,
            allow_refresh=allow_refresh,
        )
    
    def get_portfolio_value(self, skin_names: List[str], target_wear: str = None, max_float: float = None) -> float:
        """Рассчитать стоимость портфеля"""
        total = 0.0
        for skin_name in skin_names:
            price = self.get_skin_price(skin_name, target_wear, max_float)
            if price:
                total += price
        return total
    
    def get_multiple_prices(self, skin_names: List[str], target_wear: str = None, max_float: float = None) -> Dict[str, float]:
        """Получить цены для списка скинов"""
        prices = {}
        for skin_name in skin_names:
            price = self.get_skin_price(skin_name, target_wear, max_float)
            if price:
                prices[skin_name] = price
        return prices

    def get_listings(
        self,
        skin_name: str,
        target_wear: str = None,
        max_float: float = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = False,
        allow_refresh: bool = True,
        limit: int = 50,
    ) -> List[Tuple[float, Optional[float], str]]:
        # Merge listings from market cache and DMarket (which has real float data)
        market_lots = self.market_client.get_listings(
            skin_name,
            target_wear=target_wear,
            max_float=max_float,
            exclude_stattrak=exclude_stattrak,
            require_stattrak=require_stattrak,
            strict_name_match=strict_name_match,
            allow_refresh=allow_refresh,
            limit=limit,
        )

        if self.dmarket_client and bool(getattr(self.dmarket_client, 'enabled', False)):
            try:
                dm_lots = self.dmarket_client.get_listings(
                    skin_name,
                    target_wear=target_wear,
                    max_float=max_float,
                    exclude_stattrak=exclude_stattrak,
                    require_stattrak=require_stattrak,
                    limit=limit,
                )
                if dm_lots:
                    # Merge and sort by price, deduplicate by (price, float)
                    merged = list(market_lots) + list(dm_lots)
                    seen = set()
                    result = []
                    for lot in sorted(merged, key=lambda x: (x[0], 1.0 if x[1] is None else x[1])):
                        key = (round(lot[0], 2), round(lot[1], 4) if lot[1] is not None else None)
                        if key not in seen:
                            seen.add(key)
                            result.append(lot)
                    return result[:limit]
            except Exception:
                pass

        return market_lots

    def get_best_buy_with_float(
        self,
        skin_name: str,
        *,
        target_wear: str = None,
        max_float: float = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        limit: int = 50,
    ) -> Optional[Tuple[float, Optional[float], str, str]]:
        """Return cheapest available lot across all enabled sources: (price, float, wear, source)."""
        candidates = []

        # market.csgo
        try:
            lots = self.market_client.get_listings(
                skin_name,
                target_wear=target_wear,
                max_float=max_float,
                exclude_stattrak=exclude_stattrak,
                require_stattrak=require_stattrak,
                strict_name_match=False,
                allow_refresh=True,
                limit=limit,
            )
            if lots:
                p, f, w = lots[0]
                candidates.append((float(p), f, str(w or ''), 'MARKETCSGO'))
        except Exception:
            pass

        # CSFloat — real float data, useful for float-constrained buys
        if self.csfloat_client and bool(getattr(self.csfloat_client, 'enabled', False)):
            try:
                cf_lots = self.csfloat_client.get_listings(
                    skin_name,
                    target_wear=target_wear,
                    max_float=max_float,
                    exclude_stattrak=exclude_stattrak,
                    require_stattrak=require_stattrak,
                    limit=10,
                )
                if cf_lots:
                    p, f, w = cf_lots[0]
                    candidates.append((float(p), f, str(w or ''), 'CSFLOAT'))
            except Exception:
                pass

        # DMarket — additional source with real float data
        if self.dmarket_client and bool(getattr(self.dmarket_client, 'enabled', False)):
            try:
                dm_lots = self.dmarket_client.get_listings(
                    skin_name,
                    target_wear=target_wear,
                    max_float=max_float,
                    exclude_stattrak=exclude_stattrak,
                    require_stattrak=require_stattrak,
                    limit=10,
                )
                if dm_lots:
                    p, f, w = dm_lots[0]
                    candidates.append((float(p), f, str(w or ''), 'DMARKET'))
            except Exception:
                pass

        if not candidates:
            return None
        # Return cheapest lot across all sources
        return min(candidates, key=lambda x: x[0])

    def get_liquidity_metrics(
        self,
        skin_name: str,
        target_wear: str = None,
        max_float: float = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = False,
        allow_refresh: bool = True,
        depth_n: int = 10,
    ) -> Dict:
        return self.market_client.get_liquidity_metrics(
            skin_name,
            target_wear=target_wear,
            max_float=max_float,
            exclude_stattrak=exclude_stattrak,
            require_stattrak=require_stattrak,
            strict_name_match=strict_name_match,
            allow_refresh=allow_refresh,
            depth_n=depth_n,
        )

    def get_effective_sell_price(
        self,
        skin_name: str,
        target_wear: str = None,
        max_float: float = None,
        exclude_stattrak: bool = True,
        require_stattrak: bool = False,
        strict_name_match: bool = False,
        allow_refresh: bool = True,
    ) -> Optional[float]:
        """
        Returns best avg sell price across market.csgo and DMarket.
        Uses sales history (avg of recent sales) for both platforms.
        market.csgo: uses built-in liquidity-adjusted price (includes sales history).
        DMarket: uses avg of last 20 sales from trade-aggregator API.
        Returns (best_price, source) — picks whichever platform gives higher net price.
        """
        mc_fee = float(self.market_client.market_fee if hasattr(self.market_client, 'market_fee') else 0.07)
        dm_fee = float(os.getenv('DMARKET_SELL_FEE', '0.05') or 0.05)
        dm_min_sales = int(os.getenv('DMARKET_SELL_MIN_SALES', '3') or 3)
        dm_max_ratio = float(os.getenv('DMARKET_SELL_MAX_RATIO', '1.5') or 1.5)

        # ── market.csgo: liquidity-adjusted price (already uses sales history internally)
        mc_price = self.market_client.get_effective_sell_price(
            skin_name,
            target_wear=target_wear,
            max_float=max_float,
            exclude_stattrak=exclude_stattrak,
            require_stattrak=require_stattrak,
            strict_name_match=strict_name_match,
            allow_refresh=allow_refresh,
        )
        mc_net = float(mc_price) if mc_price is not None else None

        # ── DMarket: avg of last 20 sales from trade-aggregator
        dm_net = None
        if self.dmarket_client and bool(getattr(self.dmarket_client, 'enabled', False)):
            try:
                dm_sales = self.dmarket_client.get_last_sales(
                    skin_name,
                    target_wear=target_wear,
                    limit=20,
                )
                if len(dm_sales) >= dm_min_sales:
                    dm_avg = sum(dm_sales) / len(dm_sales)
                    dm_net_candidate = dm_avg * (1.0 - dm_fee)
                    # Sanity check: DMarket avg must not exceed market.csgo by more than ratio
                    if mc_net is not None:
                        if dm_net_candidate <= mc_net * dm_max_ratio:
                            dm_net = dm_net_candidate
                    else:
                        dm_net = dm_net_candidate
            except Exception:
                pass

        # Return best net price
        if mc_net is not None and dm_net is not None:
            return max(mc_net, dm_net)
        return mc_net if mc_net is not None else dm_net

    def load_bid_prices(self, force: bool = False) -> bool:
        """
        Load best buy-order prices from market.csgo class_instance/USD.json.
        Builds {normalized_name: {wear: bid_price}} for use in BID mode.
        Returns True if data was loaded successfully.
        """
        now = time.time()
        with self._bid_prices_lock:
            if not force and self._bid_prices and (now - self._bid_prices_ts) < self._bid_prices_ttl:
                return True

        ci_data = self.market_client._get_class_instance_prices()
        if not ci_data:
            return False

        try:
            min_bid_ratio = float(os.getenv('MC_SELL_MIN_BID_VS_ASK', '0.70') or 0.70)
        except Exception:
            min_bid_ratio = 0.70

        _WEAR_FLOAT_MAX = {
            'Factory New': 0.07, 'Minimal Wear': 0.15,
            'Field-Tested': 0.38, 'Well-Worn': 0.45, 'Battle-Scarred': 1.0,
        }

        new_bids: Dict[str, Dict[str, float]] = {}
        for entry in ci_data.values():
            mhn = str(entry.get('market_hash_name') or '')
            if not mhn:
                continue
            raw_bid = entry.get('buy_order')
            raw_ask = entry.get('price')
            if raw_bid is None or raw_ask is None:
                continue
            try:
                bid = float(raw_bid)
                ask = float(raw_ask)
            except Exception:
                continue
            if bid <= 0 or ask <= 0:
                continue
            if bid < ask * min_bid_ratio:
                continue
            wear = None
            for w in _WEAR_FLOAT_MAX:
                if mhn.endswith(f'({w})'):
                    wear = w
                    break
            if not wear:
                continue
            norm = self.market_client._normalize_skin_name(mhn)
            if not norm:
                continue
            if norm not in new_bids:
                new_bids[norm] = {}
            new_bids[norm][wear] = bid

        with self._bid_prices_lock:
            self._bid_prices = new_bids
            self._bid_prices_ts = time.time()

        logger.info('BID prices loaded: %d skins with valid buy orders', len(new_bids))
        return bool(new_bids)

    def get_bid_price(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        require_stattrak: bool = False,
    ) -> Optional[float]:
        """
        Return best buy-order price for a skin in BID mode.
        Returns None if no valid bid exists (use ask price as fallback).
        """
        st_prefix = 'StatTrak\u2122 ' if bool(require_stattrak) else ''
        if target_wear:
            mhn = f'{st_prefix}{skin_name} ({target_wear})'
        else:
            mhn = f'{st_prefix}{skin_name}'
        norm = self.market_client._normalize_skin_name(mhn)
        with self._bid_prices_lock:
            wears = self._bid_prices.get(norm)
        if not wears:
            return None
        if target_wear and target_wear in wears:
            return wears[target_wear]
        if wears:
            return min(wears.values())
        return None

    def suggest_request_price(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        require_stattrak: bool = False,
        buy_source: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        Suggest an optimal buy-request price for a skin.
        Routes to the correct market client based on buy_source:
          - 'DMARKET' → DMarketClient.suggest_request_price (uses DM order book + sales)
          - anything else → MarketCSGOClient.suggest_request_price (uses MC order book + sales)
        Returns None if data unavailable.
        """
        src = str(buy_source or 'MARKETCSGO').strip().upper()
        if src == 'DMARKET' and self.dmarket_client and bool(getattr(self.dmarket_client, 'enabled', False)):
            return self.dmarket_client.suggest_request_price(
                skin_name,
                target_wear=target_wear,
                require_stattrak=bool(require_stattrak),
            )
        return self.market_client.suggest_request_price(
            skin_name,
            target_wear=target_wear,
            require_stattrak=bool(require_stattrak),
        )

    def suggest_sell_price(
        self,
        skin_name: str,
        *,
        target_wear: Optional[str] = None,
        require_stattrak: bool = False,
        sell_source: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        Suggest an instant-sell price for an outcome skin.
        Routes to the correct market client based on sell_source:
          - 'DMARKET' → DMarketClient.suggest_sell_price
          - anything else → MarketCSGOClient.suggest_sell_price
        Returns None if no buy orders available.
        """
        src = str(sell_source or 'MARKETCSGO').strip().upper()
        if src == 'DMARKET' and self.dmarket_client and bool(getattr(self.dmarket_client, 'enabled', False)):
            return self.dmarket_client.suggest_sell_price(
                skin_name,
                target_wear=target_wear,
                require_stattrak=bool(require_stattrak),
            )
        return self.market_client.suggest_sell_price(
            skin_name,
            target_wear=target_wear,
            require_stattrak=bool(require_stattrak),
        )

