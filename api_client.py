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

            # Оцениваем приблизительный float по wear (середина диапазона).
            # Это позволяет max_float-фильтрам в get_price_with_float и get_listings
            # корректно отбирать предметы нужного wear-уровня вместо игнорирования фильтра.
            _WEAR_FLOAT_MID = {
                'Factory New':   0.035,
                'Minimal Wear':  0.110,
                'Field-Tested':  0.260,
                'Well-Worn':     0.415,
                'Battle-Scarred': 0.725,
            }
            item_float = _WEAR_FLOAT_MID.get(wear, None)

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
            logger.debug(f"Файл содержит прямой список из {len(items)} лотов")
        elif isinstance(file_data, dict) and 'items' in file_data:
            # API возвращает словарь с items
            items = file_data['items']
            logger.debug(f"Файл содержит словарь с {len(items)} лотов")
        else:
            logger.warning(f"Неизвестный формат данных: {type(file_data)}")
            return 0
        
        # Отладка: выводим структуру первого элемента
        if items and len(items) > 0:
            first_item = items[0]
            logger.debug(f"Структура первого лота: {type(first_item)}, длина: {len(first_item) if isinstance(first_item, list) else 'N/A'}")
            logger.debug(f"Первый лот: {first_item}")
        
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
                logger.debug(f"Ошибка обработки лота: {e}")
                continue
        
        logger.debug(f"Обработано {lots_processed} лотов из файла")
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

        if f <= 0.07:
            return "Factory New"
        if f <= 0.15:
            return "Minimal Wear"
        if f <= 0.37:
            return "Field-Tested"
        if f <= 0.44:
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

        # Если качество запрошено, но в export оно отсутствует/Unknown, то не обнуляем цену.
        # Fallback: берем самый дешевый ордер для точного имени без фильтра по wear.
        if (not all_prices) and target_wear:
            for variant in search_variants:
                if variant not in cache_snapshot:
                    continue
                for price, item_float, wear, is_stattrak in cache_snapshot[variant]:
                    if exclude_stattrak and is_stattrak:
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


class PriceManager:
    """Менеджер цен для работы с Full Export API"""
    
    def __init__(self):
        self.market_client = MarketCSGOClient()
        self.csfloat_client = CSFloatClient()
    
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
        return self.market_client.load_prices(force_refresh=force_refresh)
    
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
                    return float(val)
            except Exception:
                pass

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
        if self.csfloat_client and bool(getattr(self.csfloat_client, 'enabled', False)):
            try:
                lots = self.csfloat_client.get_listings(
                    skin_name,
                    target_wear=target_wear,
                    max_float=max_float,
                    exclude_stattrak=exclude_stattrak,
                    require_stattrak=require_stattrak,
                    limit=limit,
                )
                if lots:
                    return list(lots)
            except Exception:
                pass

        return self.market_client.get_listings(
            skin_name,
            target_wear=target_wear,
            max_float=max_float,
            exclude_stattrak=exclude_stattrak,
            require_stattrak=require_stattrak,
            strict_name_match=strict_name_match,
            allow_refresh=allow_refresh,
            limit=limit,
        )

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
        best = None
        
        if self.csfloat_client and bool(getattr(self.csfloat_client, 'enabled', False)):
            try:
                lots = self.csfloat_client.get_listings(
                    skin_name,
                    target_wear=target_wear,
                    max_float=max_float,
                    exclude_stattrak=exclude_stattrak,
                    require_stattrak=require_stattrak,
                    limit=limit,
                )
                if lots:
                    p, f, w = lots[0]
                    best = (float(p), f, str(w or ''), 'CSFLOAT')
            except Exception:
                pass

        try:
            lots2 = self.market_client.get_listings(
                skin_name,
                target_wear=target_wear,
                max_float=max_float,
                exclude_stattrak=exclude_stattrak,
                require_stattrak=require_stattrak,
                strict_name_match=False,
                allow_refresh=True,
                limit=limit,
            )
            if lots2:
                p2, f2, w2 = lots2[0]
                cand = (float(p2), f2, str(w2 or ''), 'MARKETCSGO')
                if (best is None) or float(cand[0]) + 1e-12 < float(best[0]):
                    best = cand
        except Exception:
            pass

        return best

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
        return self.market_client.get_effective_sell_price(
            skin_name,
            target_wear=target_wear,
            max_float=max_float,
            exclude_stattrak=exclude_stattrak,
            require_stattrak=require_stattrak,
            strict_name_match=strict_name_match,
            allow_refresh=allow_refresh,
        )
