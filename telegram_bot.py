import os
import time
import asyncio
import logging
import html
import urllib.parse
from collections import OrderedDict
import threading
from typing import Dict, List, Optional, Tuple
from pathlib import Path

from dotenv import load_dotenv, dotenv_values
from telegram import Update, WebAppInfo
from telegram.constants import ParseMode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

from bot_service import TargetHuntingService


_service: Optional[TargetHuntingService] = None


_DETAILS_CACHE_TTL_S = 300.0
_details_cache_lock = threading.Lock()
_details_text_cache: Dict[str, Tuple[float, str]] = {}
_craft_text_cache: Dict[str, Tuple[float, str]] = {}
_warmup_lock = threading.Lock()
_last_warmup_ts: Dict[str, float] = {}


def _cache_get(cache: Dict[str, Tuple[float, str]], key: str) -> Optional[str]:
    now = time.time()
    with _details_cache_lock:
        val = cache.get(key)
        if not val:
            return None
        ts, text = val
        if (now - float(ts)) > float(_DETAILS_CACHE_TTL_S):
            try:
                cache.pop(key, None)
            except Exception:
                pass
            return None
        return text


def _cache_set(cache: Dict[str, Tuple[float, str]], key: str, text: str) -> None:
    with _details_cache_lock:
        cache[key] = (time.time(), str(text))


def _details_cache_key(*, meta: Dict, mode: str, max_inv: Optional[float], idx: int) -> str:
    try:
        return f"{_normalize_mode(mode)}|{str(max_inv)}|{int(idx)}|{float(meta.get('timestamp') or 0.0):.3f}"
    except Exception:
        return ''


def _try_get_cached_text(*, is_craft: bool, cache_key: str) -> Optional[str]:
    if not cache_key:
        return None
    return _cache_get(_craft_text_cache if bool(is_craft) else _details_text_cache, cache_key)


def _get_warmup_cfg() -> Tuple[bool, int]:
    enable = str(os.getenv('DETAILS_WARMUP_ENABLE') or '1').strip().lower() not in {'0', 'false', 'no', 'off'}
    try:
        topn = int(os.getenv('DETAILS_WARMUP_TOPN') or 20)
    except Exception:
        topn = 20
    topn = max(0, min(int(topn), 200))
    return (bool(enable), int(topn))


def _warmup_contract_texts(*, svc: TargetHuntingService, mode: str) -> None:
    enable, topn = _get_warmup_cfg()
    if not bool(enable) or int(topn) <= 0:
        return

    # We only warm up default list (no max investment filter), since it is the primary UX path.
    max_inv = None
    results, meta = svc.get_cached(mode=_normalize_mode(mode), max_investment=max_inv, limit=200)
    if not meta.get('ready') or not results:
        return

    ts = float(meta.get('timestamp') or 0.0)
    wkey = f"{_normalize_mode(mode)}|{str(max_inv)}"
    with _warmup_lock:
        prev = float(_last_warmup_ts.get(wkey) or 0.0)
        if ts > 0.0 and abs(ts - prev) < 1e-6:
            return
        _last_warmup_ts[wkey] = ts

    n = min(int(topn), len(results))
    for i in range(1, n + 1):
        ck = _details_cache_key(meta=meta, mode=mode, max_inv=max_inv, idx=i)
        if ck and _cache_get(_details_text_cache, ck) is None:
            try:
                txt = _render_details(svc=svc, mode=mode, max_inv=max_inv, idx=i)
                _cache_set(_details_text_cache, ck, txt)
            except Exception:
                pass

        ck2 = _details_cache_key(meta=meta, mode=mode, max_inv=max_inv, idx=i)
        if ck2 and _cache_get(_craft_text_cache, ck2) is None:
            try:
                txt2 = _render_craft(svc=svc, mode=mode, max_inv=max_inv, idx=i)
                _cache_set(_craft_text_cache, ck2, txt2)
            except Exception:
                pass


async def _details_warmup_worker(app: Application) -> None:
    while True:
        try:
            svc = app.bot_data.get('svc')
            if svc is not None:
                await asyncio.to_thread(_warmup_contract_texts, svc=svc, mode='PROFIT')
                await asyncio.to_thread(_warmup_contract_texts, svc=svc, mode='SAFE')
        except Exception:
            pass
        await asyncio.sleep(3.0)

logger = logging.getLogger(__name__)

_last_net_err_log_ts: float = 0.0

PAGE_SIZE = 5


def _load_env_file_manual(dotenv_path: Path) -> dict:
    vals = {}
    try:
        raw = None
        try:
            raw = dotenv_path.read_text(encoding='utf-8-sig')
        except Exception:
            raw = dotenv_path.read_text(encoding='cp1251')
        for line in (raw or '').splitlines():
            s = (line or '').strip()
            if not s or s.startswith('#'):
                continue
            if '=' not in s:
                continue
            k, v = s.split('=', 1)
            k = (k or '')
            k = k.replace('\ufeff', '').replace('\u00a0', ' ').strip()
            # Normalize key name to avoid invisible unicode chars breaking getenv lookups.
            k = ''.join(ch for ch in k if (ch.isalnum() or ch == '_')).upper()
            v = (v or '').strip()
            if not k:
                continue
            if v and ((v[0] == v[-1]) and v[0] in {'"', "'"}):
                v = v[1:-1]
            vals[k] = v
    except Exception:
        return {}
    return vals


def _get_chat_prefs(app: Application, chat_id: int) -> dict:
    prefs = app.bot_data.setdefault('chat_prefs', {})
    cid = int(chat_id)
    p = prefs.get(cid)
    if not isinstance(p, dict):
        p = {
            'notice_enabled': False,
            'start_hidden': False,
            'notice_last_sig': None,
            'notice_last_ts': 0.0,
        }
        prefs[cid] = p
    return p


def _build_main_reply_kb(*, show_start: bool, notice_enabled: bool) -> ReplyKeyboardMarkup:
    rows = []
    if show_start:
        rows.append([KeyboardButton('Start')])
    rows.append([KeyboardButton('List'), KeyboardButton(f"Notice: {'ON' if notice_enabled else 'OFF'}")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _build_proxy_url_from_env() -> str:
    # Supported envs:
    # - TELEGRAM_PROXY_URL: full URL like http://user:pass@host:port or socks5://user:pass@host:port
    # - TELEGRAM_PROXY: custom format host:port@user:pass
    # - TELEGRAM_PROXY_TYPE: http|https|socks5 (default: http)
    proxy_url = (os.getenv('TELEGRAM_PROXY_URL') or '').strip()
    if proxy_url:
        return proxy_url

    raw = (os.getenv('TELEGRAM_PROXY') or '').strip()
    if not raw:
        return ''

    scheme = (os.getenv('TELEGRAM_PROXY_TYPE') or 'http').strip().lower()
    if scheme not in {'http', 'https', 'socks5'}:
        scheme = 'http'

    # raw: host:port@user:pass
    try:
        host_port, user_pass = raw.split('@', 1)
        host, port = host_port.split(':', 1)
        user, password = user_pass.split(':', 1)
        host = host.strip()
        port = port.strip()
        user = user.strip()
        password = password.strip()
        if not (host and port and user and password):
            return ''
        return f"{scheme}://{user}:{password}@{host}:{port}"
    except Exception:
        return ''


def _redact_proxy_url(url: str) -> str:
    try:
        s = str(url or '')
        if '://' not in s:
            return s
        scheme, rest = s.split('://', 1)
        if '@' not in rest:
            return f"{scheme}://{rest}"
        creds, host = rest.split('@', 1)
        if ':' in creds:
            user = creds.split(':', 1)[0]
            return f"{scheme}://{user}:***@{host}"
        return f"{scheme}://***@{host}"
    except Exception:
        return '<proxy>'


async def _notice_worker(app: Application) -> None:
    roi_thr = 40.0
    check_every_seconds = 90.0

    while True:
        try:
            svc = app.bot_data.get('svc')
            if svc is None:
                await asyncio.sleep(2.0)
                continue

            prefs_map = app.bot_data.get('chat_prefs') or {}
            enabled_chat_ids = [cid for cid, p in list(prefs_map.items()) if isinstance(p, dict) and bool(p.get('notice_enabled'))]
            if not enabled_chat_ids:
                await asyncio.sleep(check_every_seconds)
                continue

            results, meta = svc.get_cached(mode='PROFIT', max_investment=None, limit=200)
            if not bool(meta.get('ready')) or not results:
                await asyncio.sleep(check_every_seconds)
                continue

            best = None
            for c in list(results):
                try:
                    r = float(c.get('roi') or 0.0)
                except Exception:
                    r = 0.0
                if r + 1e-9 < float(roi_thr):
                    continue
                if best is None or r > float(best.get('roi') or 0.0) + 1e-9:
                    best = c

            if best is None:
                await asyncio.sleep(check_every_seconds)
                continue

            try:
                sig = (
                    str(best.get('target_collection') or ''),
                    str(best.get('hunt_output') or ''),
                    bool(best.get('is_stattrak')),
                    round(float(best.get('roi') or 0.0), 2),
                    round(float(best.get('input_cost') or 0.0), 2),
                )
            except Exception:
                sig = str(best)

            now = time.time()
            for cid in enabled_chat_ids:
                try:
                    p = _get_chat_prefs(app, int(cid))
                    last_sig = p.get('notice_last_sig')
                    last_ts = float(p.get('notice_last_ts') or 0.0)
                    if last_sig == sig and (now - last_ts) < 3600.0:
                        continue

                    text = _format_contract_compact(1, best)
                    await app.bot.send_message(
                        chat_id=int(cid),
                        text=f"<b>Notice</b>: ROI ≥ {roi_thr:.0f}%\n\n" + text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=_build_main_reply_kb(show_start=not bool(p.get('start_hidden')), notice_enabled=True),
                    )
                    p['notice_last_sig'] = sig
                    p['notice_last_ts'] = now
                except Exception:
                    continue

        except Exception:
            logger.debug('notice worker failed', exc_info=True)

        await asyncio.sleep(check_every_seconds)


async def _cache_ready_notifier(app: Application) -> None:
    notified = False
    while True:
        try:
            if bool(app.bot_data.get('cache_ready_notified')):
                return
            svc = app.bot_data.get('svc')
            if svc is None:
                await asyncio.sleep(2.0)
                continue
            _, meta = svc.get_cached(mode='PROFIT', max_investment=None, limit=1)
            if bool(meta.get('ready')) and float(meta.get('timestamp') or 0.0) > 0.0:
                chat_ids = list(app.bot_data.get('notify_chat_ids') or [])
                if chat_ids and (not notified):
                    for cid in chat_ids:
                        try:
                            await app.bot.send_message(
                                chat_id=cid,
                                text='<b>Cache ready</b>. You can use /hunt now.',
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception:
                            continue
                app.bot_data['cache_ready_notified'] = True
                return
        except Exception:
            logger.debug('cache ready notifier failed', exc_info=True)

        await asyncio.sleep(5.0)


def _fmt_money(x: float) -> str:
    return f"{x:,.2f}".replace(',', ' ')


def _parse_max_inv(s: str) -> Optional[float]:
    s = str(s or '').strip()
    if not s or s.lower() in {'n', 'none', 'null'}:
        return None
    try:
        s = s.replace('$', '').strip()
        s = s.replace(',', '.')
        return float(s)
    except Exception:
        return None


def _encode_max_inv(v: Optional[float]) -> str:
    if v is None:
        return 'n'
    try:
        if float(v).is_integer():
            return str(int(v))
    except Exception:
        pass
    return str(v)


def _normalize_mode(mode: str) -> str:
    raw = str(mode or 'PROFIT').strip().upper()
    raw = raw.replace('_', '-').replace(' ', '')
    if raw in {'HIGH-RISK', 'HIGHRISK', 'RISK'}:
        return 'RISK'
    return 'PROFIT'


def _format_contract_compact(i: int, c: dict) -> str:
    st = 'ST' if bool(c.get('is_stattrak')) else 'NO'
    out = str(c.get('hunt_output') or '')
    col = str(c.get('target_collection') or '')
    roi = float(c.get('roi') or 0.0)
    profit = float(c.get('net_profit') or 0.0)
    cost = float(c.get('input_cost') or 0.0)
    jr = float(c.get('jackpot_ratio') or 0.0)
    ct = float(c.get('chance_of_target') or 0.0) * 100.0
    return (
        f"<b>{i}.</b> <code>{st}</code> +{_fmt_money(profit)}$ | ROI {roi:.2f}% | cost {_fmt_money(cost)}$ | "
        f"JR {jr:.2f} | CT {ct:.1f}%\n"
        f"<i>{col}</i> → <b>{out}</b>"
    )


def _build_keyboard(*, mode: str, max_inv: Optional[float], page: int, page_count: int, ids: list[int]) -> InlineKeyboardMarkup:
    mode = _normalize_mode(mode)
    enc_max = _encode_max_inv(max_inv)
    kb = []

    kb.append([
        InlineKeyboardButton('PROFIT', callback_data=f"m|PROFIT|{enc_max}|0"),
        InlineKeyboardButton('RISK', callback_data=f"m|RISK|{enc_max}|0"),
        InlineKeyboardButton('REFRESH', callback_data=f"rf|{mode}"),
    ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('◀️', callback_data=f"p|{mode}|{enc_max}|{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{max(1, page_count)}", callback_data=f"noop"))
    if (page + 1) < page_count:
        nav.append(InlineKeyboardButton('▶️', callback_data=f"p|{mode}|{enc_max}|{page+1}"))
    kb.append(nav)

    for i in ids:
        kb.append([InlineKeyboardButton(f"Details #{i}", callback_data=f"d|{mode}|{enc_max}|{i}")])

    return InlineKeyboardMarkup(kb)


def _build_details_kb(*, mode: str, max_inv: Optional[float], idx: int) -> InlineKeyboardMarkup:
    mode = _normalize_mode(mode)
    enc_max = _encode_max_inv(max_inv)
    kb = [
        [InlineKeyboardButton(f"Craft #{int(idx)}", callback_data=f"c|{mode}|{enc_max}|{int(idx)}")],
    ]
    return InlineKeyboardMarkup(kb)


def _market_csgo_search_url(*, skin_name: str, wear: str, is_stattrak: bool) -> str:
    # Variant B: open a MarketCSGO search page. Float is shown as a hint next to the link.
    base = (os.getenv('MARKET_CSGO_WEB_SEARCH_URL_TPL') or 'https://market.csgo.com/en/?search={query}').strip()
    nm = str(skin_name or '').strip()
    if is_stattrak and nm and ('stattrak' not in nm.lower()):
        nm = 'StatTrak™ ' + nm
    wr = str(wear or '').strip()
    if wr and nm and ('(' not in nm):
        nm = f"{nm} ({wr})"
    q = urllib.parse.quote_plus(nm)
    return base.format(query=q)


def _csfloat_search_url(*, skin_name: str, wear: str, is_stattrak: bool) -> str:
    base = (os.getenv('CSFLOAT_WEB_SEARCH_URL_TPL') or 'https://csfloat.com/search?query={query}').strip()
    nm = str(skin_name or '').strip()
    if is_stattrak and nm and ('stattrak' not in nm.lower()):
        nm = 'StatTrak™ ' + nm
    wr = str(wear or '').strip()
    if wr and nm and ('(' not in nm):
        nm = f"{nm} ({wr})"
    q = urllib.parse.quote_plus(nm)
    return base.format(query=q)


def _wear_to_float_range(wear: str) -> Tuple[float, float]:
    w = str(wear or '').strip()
    if w == 'Factory New':
        return (0.0, 0.07)
    if w == 'Minimal Wear':
        return (0.07, 0.15)
    if w == 'Field-Tested':
        return (0.15, 0.38)
    if w == 'Well-Worn':
        return (0.38, 0.45)
    if w == 'Battle-Scarred':
        return (0.45, 1.0)
    return (0.0, 1.0)


_WEAR_ORDER = ['Factory New', 'Minimal Wear', 'Field-Tested', 'Well-Worn', 'Battle-Scarred']
_WEAR_THRESHOLDS = {
    'Factory New': 0.07,
    'Minimal Wear': 0.15,
    'Field-Tested': 0.38,
    'Well-Worn': 0.45,
    'Battle-Scarred': 1.0,
}


def _wear_idx(wear: str) -> int:
    w = str(wear or '').strip()
    try:
        return _WEAR_ORDER.index(w)
    except Exception:
        return len(_WEAR_ORDER) - 1


def _ceil3(x: float) -> float:
    try:
        v = float(x)
    except Exception:
        return 0.0
    return (int(v * 1000.0 + 0.999999)) / 1000.0


def _floor3(x: float) -> float:
    try:
        v = float(x)
    except Exception:
        return 0.0
    return (int(v * 1000.0)) / 1000.0


def _calc_avg_norm_threshold_for_all_outcomes(*, svc: TargetHuntingService, contract: Dict, fallback_target_wear: str) -> Tuple[Optional[float], bool]:
    # Returns (thr, ok). If ok=False, strict guarantee is impossible for all outcomes (wears_avail constraints).
    # We compute threshold per-outcome using its expected wear (as shown in Outcomes list). This is stricter
    # than hunt_expected_wear in cases where some outcomes are expected to be better than the main target.
    if not svc.calculator:
        return (None, False)

    try:
        outs = svc.calculator.calculate_contract_outcomes_details(
            contract.get('input_skins') or [],
            is_stattrak=bool(contract.get('is_stattrak')),
        )
    except Exception:
        outs = []

    outs = list(outs or [])
    if not outs:
        return (None, False)

    thr_global = 1.0
    for o in outs:
        nm = str(o.get('name') or '')
        o_target_wear = str(o.get('wear') or '').strip() or str(fallback_target_wear or '').strip()
        t_idx = _wear_idx(o_target_wear)
        skin_data = None
        try:
            skin_data = svc.calculator.database.get_skin_by_name(nm)
        except Exception:
            skin_data = None
        if not skin_data:
            # Without min/max, can't guarantee
            return (None, False)

        try:
            min_f = float(skin_data.min_float)
            max_f = float(skin_data.max_float)
        except Exception:
            return (None, False)

        denom = float(max_f - min_f)
        if denom <= 1e-9:
            return (None, False)

        try:
            wears_avail = list(getattr(skin_data, 'wears', None) or [])
        except Exception:
            wears_avail = []

        # Determine which computed wear buckets are acceptable given target_wear AND availability degradation.
        # Condition: computed_wear_idx <= max_allowed_idx, where max_allowed_idx is the worst available wear
        # still not worse than target.
        allowed_idxs = []
        if wears_avail:
            for w in wears_avail:
                wi = _wear_idx(w)
                if wi <= t_idx:
                    allowed_idxs.append(wi)
        else:
            # If no info about available wears, assume all wears are possible.
            allowed_idxs = list(range(0, t_idx + 1))

        if not allowed_idxs:
            return (None, False)

        max_allowed_idx = max(allowed_idxs)
        max_allowed_wear = _WEAR_ORDER[max_allowed_idx]
        max_out_float_ok = float(_WEAR_THRESHOLDS.get(max_allowed_wear, 1.0))

        thr_i = (max_out_float_ok - float(min_f)) / float(denom)
        if thr_i < 0.0:
            thr_i = 0.0
        if thr_i > 1.0:
            thr_i = 1.0
        if thr_i < thr_global:
            thr_global = thr_i

    return (float(thr_global), True)


def _fmt_float3(x: float) -> str:
    try:
        return f"{float(x):.3f}"
    except Exception:
        return 'N/A'


def _render_list(*, svc: TargetHuntingService, mode: str, max_inv: Optional[float], page: int) -> Tuple[str, InlineKeyboardMarkup]:
    mode = _normalize_mode(mode)
    results, meta = svc.get_cached(mode=mode, max_investment=max_inv, limit=200)

    if mode == 'PROFIT' and results:
        try:
            results = list(results)
            results.sort(key=lambda x: float(x.get('net_profit') or 0.0), reverse=True)
        except Exception:
            pass

    now = time.time()
    ts = float(meta.get('timestamp') or 0.0)
    age_min = ((now - ts) / 60.0) if ts else 0.0
    refreshing = bool(meta.get('refreshing'))
    last_err = meta.get('last_error')

    if not meta.get('ready'):
        txt = (
            f"<b>Target Hunting</b> — <code>{mode}</code>\n"
            f"Cache is not ready yet. {'Refreshing in background.' if refreshing else 'Preparing in background.'}"
        )
        kb = _build_keyboard(mode=mode, max_inv=max_inv, page=0, page_count=1, ids=[])
        return (txt, kb)

    total = len(results)
    if total <= 0:
        txt = (
            f"<b>Target Hunting</b> — <code>{mode}</code>\n"
            f"No contracts found for these filters.\n"
            f"<i>cache age:</i> {age_min:.1f} min"
        )
        kb = _build_keyboard(mode=mode, max_inv=max_inv, page=0, page_count=1, ids=[])
        return (txt, kb)

    page = max(0, int(page))
    page_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page >= page_count:
        page = page_count - 1

    start = page * PAGE_SIZE
    chunk = results[start: start + PAGE_SIZE]

    header = f"<b>Target Hunting</b> — <code>{mode}</code>"
    if max_inv is not None:
        header += f" | max: <code>{_fmt_money(float(max_inv))}$</code>"
    header += "\n"
    header += f"<i>cache:</i> {age_min:.1f} min"
    if refreshing:
        header += " | <i>refreshing…</i>"
    if last_err:
        header += "\n<i>last error:</i> " + str(last_err)

    lines = [header]
    ids = []
    for idx, c in enumerate(chunk, start=start + 1):
        ids.append(idx)
        lines.append(_format_contract_compact(idx, c))

    kb = _build_keyboard(mode=mode, max_inv=max_inv, page=page, page_count=page_count, ids=ids)
    return ("\n\n".join(lines), kb)


def _render_details(*, svc: TargetHuntingService, mode: str, max_inv: Optional[float], idx: int) -> str:
    mode = _normalize_mode(mode)
    results, meta = svc.get_cached(mode=mode, max_investment=max_inv, limit=200)
    if not meta.get('ready') or not results:
        return "Cache is not ready yet. Try again in a moment."

    try:
        cache_key = f"{mode}|{str(max_inv)}|{int(idx)}|{float(meta.get('timestamp') or 0.0):.3f}"
    except Exception:
        cache_key = ''
    if cache_key:
        cached_txt = _cache_get(_details_text_cache, cache_key)
        if cached_txt:
            return cached_txt

    if mode == 'PROFIT' and results:
        try:
            results = list(results)
            results.sort(key=lambda x: float(x.get('net_profit') or 0.0), reverse=True)
        except Exception:
            pass
    if mode == 'SAFE' and results:
        try:
            results = list(results)
            results.sort(key=lambda x: float(x.get('roi') or 0.0), reverse=True)
        except Exception:
            pass

    if idx <= 0 or idx > len(results):
        return "Contract not found (index out of range)."

    c = results[idx - 1]
    st = 'StatTrak' if bool(c.get('is_stattrak')) else 'Normal'
    roi = float(c.get('roi') or 0.0)
    profit = float(c.get('net_profit') or 0.0)
    cost = float(c.get('input_cost') or 0.0)
    ev = float(c.get('expected_output') or 0.0)
    outs = svc.get_contract_outcomes(c, top_n=0)
    try:
        pp_raw = 0.0
        for o in (outs or []):
            pr = float(o.get('price') or 0.0)
            pb = float(o.get('probability') or 0.0)
            if pr > float(cost) + 1e-12:
                pp_raw += pb
        pp = float(pp_raw) * 100.0
    except Exception:
        pp = float(c.get('profit_probability') or 0.0) * 100.0
    jr = float(c.get('jackpot_ratio') or 0.0)
    ct = float(c.get('chance_of_target') or 0.0) * 100.0
    col = str(c.get('target_collection') or '')
    out = str(c.get('hunt_output') or '')
    core = f"{int(c.get('main_skins_count') or 0)}/{int(c.get('filler_skins_count') or 0)}"

    lines = []
    lines.append(f"<b>#{idx} — {mode}</b> <code>{st}</code>")
    lines.append(f"<i>{col}</i> → <b>{out}</b>")
    lines.append(
        f"+{_fmt_money(profit)}$ | ROI {roi:.2f}% | cost {_fmt_money(cost)}$ | EV {_fmt_money(ev)}$\n"
        f"PP {pp:.1f}% | JR {jr:.2f} | CT {ct:.1f}% | core {core}"
    )

    ins = list(c.get('input_skins') or [])
    if ins:
        lines.append("")
        lines.append("<b>Inputs</b>")
        grouped = []
        cur = None
        for s in ins:
            name = str(s.get('name') or '')
            coll = str(s.get('collection') or '')
            key = (name, coll)
            if cur is None or cur['key'] != key:
                cur = {
                    'key': key,
                    'name': name,
                    'collection': coll,
                    'count': 0,
                    'total_price': 0.0,
                    'floats': [],
                    'wears': [],
                }
                grouped.append(cur)

            cur['count'] += 1
            cur['total_price'] += float(s.get('price') or 0.0)
            fl = s.get('float', None)
            if fl is not None:
                try:
                    cur['floats'].append(float(fl))
                except Exception:
                    pass
            w = s.get('wear', None)
            if w is not None:
                try:
                    ww = str(w).strip()
                except Exception:
                    ww = ''
                if ww:
                    cur['wears'].append(ww)

        pos = 1
        for g in grouped:
            start_i = pos
            end_i = pos + int(g['count']) - 1
            pos = end_i + 1
            idx_txt = f"{start_i:02d}-{end_i:02d}." if end_i > start_i else f"{start_i:02d}."
            avg_f = None
            if g['floats']:
                avg_f = sum(g['floats']) / float(len(g['floats']))
            f_txt = "N/A" if avg_f is None else f"{float(avg_f):.4f}"
            x_txt = f" ({int(g['count'])}x)" if int(g['count']) > 1 else ""

            wear_txt = ''
            try:
                wears = list(g.get('wears') or [])
                if wears:
                    wear_order = ['Factory New', 'Minimal Wear', 'Field-Tested', 'Well-Worn', 'Battle-Scarred']
                    uniq = list(dict.fromkeys([str(w).strip() for w in wears if str(w).strip()]))
                    if len(uniq) == 1:
                        wear_txt = f" ({uniq[0]})"
                    else:
                        # Prefer deterministic ordering when multiple wears exist.
                        def _w_key(w: str) -> int:
                            try:
                                return wear_order.index(w)
                            except Exception:
                                return 999
                        uniq.sort(key=_w_key)
                        wear_txt = f" ({'/'.join(uniq)})"
            except Exception:
                wear_txt = ''

            per_item_txt = ''
            try:
                cnt = int(g.get('count') or 0)
                if cnt > 1:
                    per_item = float(g.get('total_price') or 0.0) / float(cnt)
                    per_item_txt = f" (~{_fmt_money(per_item)}$ ea)"
            except Exception:
                per_item_txt = ''

            lines.append(
                f"{idx_txt} {g['name']}{wear_txt}{x_txt} — {_fmt_money(float(g['total_price']))}$" +
                f"{per_item_txt} | avg. f {f_txt} | {g['collection']}"
            )

    if outs:
        lines.append("")
        lines.append("<b>Outcomes</b>")
        for o in outs:
            nm = str(o.get('name') or '')
            pr = float(o.get('price') or 0.0)
            pb = float(o.get('probability') or 0.0) * 100.0
            wr = str(o.get('wear') or '')
            lines.append(f"- {nm} — {_fmt_money(pr)}$ | {pb:.2f}% | {wr}")

    out_txt = "\n".join(lines)
    if cache_key:
        _cache_set(_details_text_cache, cache_key, out_txt)
    return out_txt


def _render_craft(*, svc: TargetHuntingService, mode: str, max_inv: Optional[float], idx: int) -> str:
    mode = _normalize_mode(mode)
    results, meta = svc.get_cached(mode=mode, max_investment=max_inv, limit=200)
    if not meta.get('ready') or not results:
        return "Cache is not ready yet. Try again in a moment."

    try:
        cache_key = f"{mode}|{str(max_inv)}|{int(idx)}|{float(meta.get('timestamp') or 0.0):.3f}"
    except Exception:
        cache_key = ''
    if cache_key:
        cached_txt = _cache_get(_craft_text_cache, cache_key)
        if cached_txt:
            return cached_txt

    if mode == 'PROFIT' and results:
        try:
            results = list(results)
            results.sort(key=lambda x: float(x.get('net_profit') or 0.0), reverse=True)
        except Exception:
            pass
    if mode == 'SAFE' and results:
        try:
            results = list(results)
            results.sort(key=lambda x: float(x.get('roi') or 0.0), reverse=True)
        except Exception:
            pass

    if idx <= 0 or idx > len(results):
        return "Contract not found (index out of range)."

    c = results[idx - 1]
    st_lbl = 'StatTrak' if bool(c.get('is_stattrak')) else 'Normal'
    out = str(c.get('hunt_output') or '')
    col = str(c.get('target_collection') or '')
    expected_wear = str(c.get('hunt_expected_wear') or '')
    avg_norm_thr, thr_ok = _calc_avg_norm_threshold_for_all_outcomes(svc=svc, contract=c, fallback_target_wear=expected_wear)

    lines = []
    lines.append(f"<b>Craft #{int(idx)} — {mode}</b> <code>{st_lbl}</code>")
    lines.append(f"<i>{html.escape(col)}</i> → <b>{html.escape(out)}</b>")

    ins = list(c.get('input_skins') or [])
    if not ins:
        lines.append("")
        lines.append("No inputs found.")
        return "\n".join(lines)

    # Group identical inputs by (name, wear, collection)
    groups = OrderedDict()
    for s in ins:
        nm = str(s.get('name') or '')
        wr = str(s.get('wear') or '')
        coll = str(s.get('collection') or '')
        key = (nm, wr, coll)
        g = groups.get(key)
        if g is None:
            g = {
                'name': nm,
                'wear': wr,
                'collection': coll,
                'count': 0,
                'total_price': 0.0,
                'buy_source': None,
            }
            groups[key] = g
        g['count'] = int(g.get('count') or 0) + 1
        if not g.get('buy_source'):
            g['buy_source'] = s.get('buy_source')
        try:
            g['total_price'] = float(g.get('total_price') or 0.0) + float(s.get('price') or 0.0)
        except Exception:
            pass

    st = bool(c.get('is_stattrak'))
    st_short = 'ST' if st else 'NO'

    lines.append("")
    i = 1
    for g in groups.values():
        nm = str(g.get('name') or '')
        wr_txt = str(g.get('wear') or '').strip() or 'N/A'
        cnt = int(g.get('count') or 0)
        total_price = float(g.get('total_price') or 0.0)

        in_min, in_max = _wear_to_float_range(wr_txt)
        min_ok = float(in_min)
        max_ok = float(in_max)
        guaranteed = True

        if not thr_ok or avg_norm_thr is None:
            guaranteed = False
        else:
            # Convert avg_norm threshold into raw float bound for this input skin using its min/max float range.
            skin_data = None
            try:
                if svc.calculator:
                    skin_data = svc.calculator.database.get_skin_by_name(nm)
            except Exception:
                skin_data = None

            if not skin_data:
                guaranteed = False
            else:
                try:
                    smin = float(skin_data.min_float)
                    smax = float(skin_data.max_float)
                except Exception:
                    guaranteed = False
                else:
                    denom = float(smax - smin)
                    if denom <= 1e-9:
                        guaranteed = False
                    else:
                        max_by_norm = float(smin) + float(avg_norm_thr) * float(denom)
                        # Intersect with wear bracket
                        min_ok = max(float(in_min), float(smin))
                        max_ok = min(float(in_max), float(max_by_norm))

        # Conservative rounding to 0.001 (never over-promise)
        min_disp = _ceil3(min_ok)
        max_disp = _floor3(max_ok)
        if min_disp > max_disp + 1e-12:
            guaranteed = False
            min_disp = _ceil3(float(in_min))
            max_disp = _floor3(float(in_max))

        buy_source = str(g.get('buy_source') or '').strip().upper()
        if buy_source == 'CSFLOAT':
            url = _csfloat_search_url(skin_name=nm, wear=(wr_txt if wr_txt != 'N/A' else ''), is_stattrak=st)
        else:
            url = _market_csgo_search_url(skin_name=nm, wear=(wr_txt if wr_txt != 'N/A' else ''), is_stattrak=st)
        buy = f"<a href=\"{html.escape(url)}\">buy</a>"

        rng = f"{_fmt_float3(min_disp)}-{_fmt_float3(max_disp)}"
        if guaranteed:
            rng_txt = f"({rng})"
        else:
            rng_txt = f"({rng}, not guaranteed)"

        lines.append(
            f"<b>{i:02d}.</b> <code>{st_short}</code> {cnt}x {html.escape(nm)} ({html.escape(wr_txt)}) | "
            f"{_fmt_money(total_price)}$ | {buy} {rng_txt}"
        )

        # Request price suggestion from order book
        try:
            pm = getattr(svc, 'price_manager', None)
            if pm is not None and buy_source != 'CSFLOAT':
                wear_for_req = wr_txt if wr_txt not in ('N/A', '') else None
                req = pm.suggest_request_price(nm, target_wear=wear_for_req,
                                               require_stattrak=st, buy_source=buy_source)
                if req and req.get('suggested_price') is not None:
                    sp = float(req['suggested_price'])
                    ba = float(req['best_ask'])
                    sv_pct = float(req.get('savings_pct') or 0.0)
                    median = req.get('sales_median')
                    median_txt = f" median ${_fmt_money(median)}" if median is not None else ""
                    lines.append(
                        f"   Request: <b>${_fmt_money(sp)}</b> "
                        f"(ask ${_fmt_money(ba)},{median_txt} save {sv_pct:.1f}%)"
                    )
        except Exception:
            pass

        i += 1

    out_txt = "\n".join(lines)
    if cache_key:
        _cache_set(_craft_text_cache, cache_key, out_txt)
    return out_txt


async def cmd_app(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a button that opens the Telegram Mini App."""
    assert update.message
    webapp_url = (os.getenv('WEBAPP_URL') or '').strip()
    if not webapp_url:
        await update.message.reply_text(
            'Mini App URL is not configured.\n'
            'Set WEBAPP_URL in .env and restart the server.',
        )
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton('Open Crafty Scanner', web_app=WebAppInfo(url=webapp_url)),
    ]])
    await update.message.reply_text(
        '<b>Crafty CS2 Scanner</b>\n'
        'Tap the button below to open the Mini App:',
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    try:
        cid_dbg = int(update.effective_chat.id) if update.effective_chat else None
        logger.info('Received /start (chat_id=%s)', str(cid_dbg) if cid_dbg is not None else 'N/A')
    except Exception:
        pass
    try:
        cid = update.effective_chat.id if update.effective_chat else None
        if cid is not None:
            p = _get_chat_prefs(context.application, int(cid))
            p['start_hidden'] = True
    except Exception:
        pass
    await update.message.reply_text(
        "CS2 Craft Scanner bot\n\n"
        "Commands:\n"
        "/app — Open Mini App\n"
        "/hunt [mode] [max_investment]\n"
        "/status\n\n"
        "Modes: PROFIT, RISK\n"
        "Examples:\n"
        "/hunt\n"
        "/hunt risk\n"
        "/hunt profit 500\n"
        ,
        reply_markup=_build_main_reply_kb(
            show_start=False,
            notice_enabled=bool(_get_chat_prefs(context.application, int(update.effective_chat.id)).get('notice_enabled')) if update.effective_chat else False,
        ),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    try:
        cid = update.effective_chat.id if update.effective_chat else None
        if cid is not None:
            context.application.bot_data.setdefault('notify_chat_ids', set()).add(int(cid))
            _get_chat_prefs(context.application, int(cid))
    except Exception:
        pass
    svc = context.application.bot_data.get('svc')
    if not svc:
        await update.message.reply_text('Service not initialized')
        return

    st = svc.cache_status()
    lines = []
    now = time.time()
    for k in sorted(st.keys()):
        ts = float(st[k].get('timestamp') or 0.0)
        age_min = ((now - ts) / 60.0) if ts else 0.0
        refreshing = bool(st[k].get('refreshing'))
        cnt = int(st[k].get('count') or 0)
        last_err = st[k].get('last_error')
        s = f"{k}: {cnt} items, age {age_min:.1f} min"
        if refreshing:
            s += " (refreshing)"
        if last_err:
            s += f" | last_error={last_err}"
        lines.append(s)

    prefs_notice = False
    prefs_show_start = True
    try:
        if update.effective_chat:
            p = _get_chat_prefs(context.application, int(update.effective_chat.id))
            prefs_notice = bool(p.get('notice_enabled'))
            prefs_show_start = not bool(p.get('start_hidden'))
    except Exception:
        prefs_notice = False
        prefs_show_start = True

    await update.message.reply_text(
        "\n".join(lines) if lines else 'No cache yet',
        reply_markup=_build_main_reply_kb(show_start=prefs_show_start, notice_enabled=prefs_notice),
    )


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Принудительное обновление кэша цен и контрактов"""
    assert update.message
    svc = context.application.bot_data.get('svc')
    if not svc:
        await update.message.reply_text('Service not available')
        return

    await update.message.reply_text('Refresh started in background...')
    threading.Thread(target=svc.refresh_background, daemon=True).start()


async def cmd_hunt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    try:
        cid = update.effective_chat.id if update.effective_chat else None
        if cid is not None:
            context.application.bot_data.setdefault('notify_chat_ids', set()).add(int(cid))
            p = _get_chat_prefs(context.application, int(cid))
            p['start_hidden'] = True
    except Exception:
        pass
    svc = context.application.bot_data.get('svc')
    if not svc:
        await update.message.reply_text('Service not initialized')
        return

    args = context.args or []
    if len(args) > 2:
        await update.message.reply_text(
            "Usage: /hunt [mode] [max_investment]\n"
            "Modes: PROFIT, RISK\n"
            "Examples: /hunt | /hunt risk | /hunt profit 500",
        )
        return
    mode = _normalize_mode(args[0] if len(args) >= 1 else 'PROFIT')
    max_inv = None
    if len(args) >= 2:
        raw_max = str(args[1] or '').strip()
        max_inv = _parse_max_inv(raw_max)
        if raw_max and max_inv is None:
            await update.message.reply_text(
                "Invalid max_investment value. Expected a number (e.g. 500 or 12.5) or 'n'.\n"
                "Example: /hunt profit 500",
            )
            return
        if max_inv is not None and float(max_inv) <= 0.0:
            await update.message.reply_text(
                "max_investment must be a positive number (or omit it for no limit).",
            )
            return
    text, kb = _render_list(svc=svc, mode=mode, max_inv=max_inv, page=0)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_text_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    try:
        txt = str(update.message.text or '')
        logger.info('Received text button (chat_id=%s, text=%s)', str(update.effective_chat.id) if update.effective_chat else 'N/A', txt)
    except Exception:
        pass
    cid = update.effective_chat.id if update.effective_chat else None
    if cid is None:
        return

    txt = str(update.message.text or '').strip()
    prefs = _get_chat_prefs(context.application, int(cid))

    if txt.lower() == 'start':
        prefs['start_hidden'] = True
        await cmd_start(update, context)
        return

    if txt.lower() == 'list':
        prefs['start_hidden'] = True
        context.args = []
        await cmd_hunt(update, context)
        return

    if txt.lower().startswith('notice'):
        cur = bool(prefs.get('notice_enabled'))
        prefs['notice_enabled'] = (not cur)
        prefs['start_hidden'] = True
        await update.message.reply_text(
            f"Notice is now <b>{'ON' if bool(prefs.get('notice_enabled')) else 'OFF'}</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=_build_main_reply_kb(show_start=False, notice_enabled=bool(prefs.get('notice_enabled'))),
        )
        return


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    svc = context.application.bot_data.get('svc')
    if not svc:
        return

    data = (q.data or '').strip()
    if not data or data == 'noop':
        return

    parts = data.split('|')
    kind = parts[0]

    if kind not in {'m', 'p', 'd', 'c', 'rf'}:
        logger.warning('Unknown callback kind: %s', data)
        return

    try:
        if kind in {'m', 'p'}:
            if len(parts) < 4:
                return
            mode = _normalize_mode(parts[1])
            max_inv = _parse_max_inv(parts[2]) if len(parts) >= 3 else None
            page = int(parts[3]) if len(parts) >= 4 else 0
            text, kb = _render_list(svc=svc, mode=mode, max_inv=max_inv, page=page)
            try:
                await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception as e:
                # Telegram returns BadRequest if you try to edit message with exactly same content/markup.
                msg = str(e or '')
                if 'message is not modified' in msg.lower():
                    return
                raise
            return

        if kind == 'd':
            if len(parts) < 4:
                return
            mode = _normalize_mode(parts[1])
            max_inv = _parse_max_inv(parts[2]) if len(parts) >= 3 else None
            idx = int(parts[3]) if len(parts) >= 4 else 0
            kb = _build_details_kb(mode=mode, max_inv=max_inv, idx=idx)

            # Fast path: if pre-rendered text exists, reply immediately with no Loading message.
            try:
                _, meta = svc.get_cached(mode=mode, max_investment=max_inv, limit=200)
                ck = _details_cache_key(meta=meta, mode=mode, max_inv=max_inv, idx=idx)
                cached_txt = _try_get_cached_text(is_craft=False, cache_key=ck)
            except Exception:
                cached_txt = None
            if cached_txt:
                await q.message.reply_text(cached_txt, parse_mode=ParseMode.HTML, reply_markup=kb)
                return

            loading = await q.message.reply_text("Loading…", parse_mode=ParseMode.HTML)
            start = time.time()
            text = await asyncio.to_thread(_render_details, svc=svc, mode=mode, max_inv=max_inv, idx=idx)
            try:
                await loading.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    # Убираем спам логи о рендеринге деталей
    # try:
    #     logger.info('Details render done: idx=%s mode=%s in %.2fs', str(idx), str(mode), time.time() - start)
    # except Exception:
    #     pass
            return

        if kind == 'c':
            if len(parts) < 4:
                return
            mode = _normalize_mode(parts[1])
            max_inv = _parse_max_inv(parts[2]) if len(parts) >= 3 else None
            idx = int(parts[3]) if len(parts) >= 4 else 0
            # Fast path: if pre-rendered craft exists, reply immediately with no Loading message.
            try:
                _, meta = svc.get_cached(mode=mode, max_investment=max_inv, limit=200)
                ck = _details_cache_key(meta=meta, mode=mode, max_inv=max_inv, idx=idx)
                cached_txt = _try_get_cached_text(is_craft=True, cache_key=ck)
            except Exception:
                cached_txt = None
            if cached_txt:
                await q.message.reply_text(cached_txt, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                return

            loading = await q.message.reply_text("Loading…", parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            start = time.time()
            text = await asyncio.to_thread(_render_craft, svc=svc, mode=mode, max_inv=max_inv, idx=idx)
            try:
                await loading.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception:
                await q.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    # Убираем спам логи о рендеринге craft
    # try:
    #     logger.info('Craft render done: idx=%s mode=%s in %.2fs', str(idx), str(mode), time.time() - start)
    # except Exception:
    #     pass
            return

        if kind == 'rf':
            mode = _normalize_mode(parts[1] if len(parts) >= 2 else 'PROFIT')
            logger.info('Manual refresh requested: mode=%s', mode)
            if svc is None:
                await q.answer('Service not available', show_alert=True)
                return
            threading.Thread(target=svc.refresh_mode, args=(mode,), daemon=True).start()
            await q.answer(f'Refreshing {mode} mode...', show_alert=False)
            return
    except Exception:
        logger.exception('Failed to handle callback: %s', data)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Keep it short; detailed traceback still goes to file via root logger.
    global _last_net_err_log_ts
    try:
        err = context.error
    except Exception:
        err = None

    msg = str(err or '')
    low = msg.lower()
    err_type = type(err).__name__ if err else 'unknown'

    # КРИТИЧНО: ConflictError означает что бот запущен в двух местах одновременно
    if 'conflict' in low or err_type.lower() == 'conflict':
        logger.critical(
            'CONFLICT ERROR: Bot is already running in another process! '
            'Stop all other bot instances.'
        )
        import sys
        sys.exit(1)

    now = time.time()
    # Throttle repeating network errors.
    if 'connect' in low or 'timeout' in low or 'networkerror' in low:
        if (now - float(_last_net_err_log_ts or 0.0)) >= 60.0:
            _last_net_err_log_ts = now
            logger.error('Telegram network error (retrying): %s', err_type)
        return

    logger.error('Unhandled bot error: %s', err_type)


def main() -> None:
    log_path = Path(__file__).resolve().with_name('telegram_bot.log')

    class _ConsoleAllowlistFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            name = str(record.name or '')
            msg = str(record.getMessage() or '')

            # Always show errors and warnings from any module
            if record.levelno >= logging.WARNING:
                return True

            # Bot/service startup and refresh status
            if name.startswith('bot_service') or name == '__main__' or name.startswith('webapp_server'):
                # Skip verbose per-item debug lines
                noisy = ('HuntDebug', 'HuntProfile', 'HuntDebugEval', 'HuntDebugInputs',
                         'RankTargets', 'SalesHistory', 'TargetHuntingService env')
                if any(n in msg for n in noisy):
                    return False
                return True

            # Telegram framework startup only
            if name.startswith('telegram.ext'):
                return 'Application started' in msg or 'Application stopped' in msg

            return False

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter('%(asctime)s %(levelname)s:%(name)s:%(threadName)s:%(message)s')

    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    ch.addFilter(_ConsoleAllowlistFilter())

    root.addHandler(fh)
    root.addHandler(ch)

    # Reduce console/file noise from very chatty modules; file still has DEBUG available.
    logging.getLogger('api_client').setLevel(logging.INFO)
    logging.getLogger('calculator').setLevel(logging.INFO)
    logging.getLogger('telegram').setLevel(logging.WARNING)
    logging.getLogger('telegram.ext').setLevel(logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)

    dotenv_path = Path(__file__).resolve().with_name('.env')
    load_dotenv(dotenv_path=dotenv_path, override=True)
    # Some environments (notably certain Windows launchers/IDEs) may ignore python-dotenv parsing.
    # To make startup deterministic, also parse via dotenv_values and inject missing keys.
    try:
        if os.getenv('HUNT_DEBUG') is None:
            vals = dict(dotenv_values(dotenv_path) or {})
            manual_vals = dict(_load_env_file_manual(dotenv_path) or {})
            # Merge manual values in case python-dotenv skipped some keys.
            for k, v in manual_vals.items():
                if k not in vals:
                    vals[k] = v
            has_hunt_debug = 'HUNT_DEBUG' in (vals or {})
            for k, v in (vals or {}).items():
                if v is None:
                    continue
                if os.getenv(k) is None:
                    os.environ[str(k)] = str(v)
            try:
                hunt_keys = [repr(k) for k in (vals or {}).keys() if str(k).upper().startswith('HUNT')]
            except Exception:
                hunt_keys = []
            logging.getLogger().info(
                'Env fallback used: parsed_keys=%s hunt_debug_key_present=%s hunt_keys=%s',
                int(len(vals or {})),
                'Y' if has_hunt_debug else 'N',
                str(hunt_keys),
            )
    except Exception:
        pass
    logging.getLogger().info('Loaded env file: %s (HUNT_DEBUG=%s)', str(dotenv_path), str(os.getenv('HUNT_DEBUG')))
    token = (os.getenv('TELEGRAM_BOT_TOKEN') or '').strip()
    if not token:
        try:
            vals = dotenv_values(dotenv_path)
            token = (vals.get('TELEGRAM_BOT_TOKEN') or '').strip()
        except Exception:
            token = ''
    
    # Проверяем валидность токена
    if not token or token == 'YOUR_NEW_TOKEN_HERE':
        logger.error('TELEGRAM_BOT_TOKEN is not set or invalid!')
        logger.error('Get a token from @BotFather in Telegram (/newbot), then set it in .env')
        raise RuntimeError('TELEGRAM_BOT_TOKEN is not set or invalid in .env')

    if ':' not in token or len(token.split(':')) != 2:
        logger.error('TELEGRAM_BOT_TOKEN has invalid format (expected: 123456789:ABC-DEF...)')
        raise RuntimeError('TELEGRAM_BOT_TOKEN has invalid format')

    global _service
    _service = TargetHuntingService()
    _service.initialize()  # initialize() already calls start_refresher() internally

    logger.info('Telegram bot starting...')

    async def _post_init(app: Application) -> None:
        app.bot_data.setdefault('notify_chat_ids', set())
        app.bot_data.setdefault('cache_ready_notified', False)
        app.bot_data.setdefault('chat_prefs', {})
        # Создаем задачи после полной инициализации приложения
        asyncio.create_task(_cache_ready_notifier(app))
        asyncio.create_task(_notice_worker(app))
        asyncio.create_task(_details_warmup_worker(app))

    proxy_url = _build_proxy_url_from_env()
    req_kwargs = {
        'connect_timeout': float(os.getenv('TELEGRAM_CONNECT_TIMEOUT') or 30.0),
        'read_timeout': float(os.getenv('TELEGRAM_READ_TIMEOUT') or 30.0),
        'write_timeout': float(os.getenv('TELEGRAM_WRITE_TIMEOUT') or 30.0),
        'pool_timeout': float(os.getenv('TELEGRAM_POOL_TIMEOUT') or 30.0),
    }
    get_updates_kwargs = {
        'connect_timeout': float(os.getenv('TELEGRAM_CONNECT_TIMEOUT') or 60.0),
        'read_timeout': float(os.getenv('TELEGRAM_GETUPDATES_READ_TIMEOUT') or 180.0),
        'write_timeout': float(os.getenv('TELEGRAM_WRITE_TIMEOUT') or 60.0),
        'pool_timeout': float(os.getenv('TELEGRAM_GETUPDATES_POOL_TIMEOUT') or 180.0),
    }
    if proxy_url:
        req_kwargs['proxy'] = proxy_url
        get_updates_kwargs['proxy'] = proxy_url

    logger.info('Telegram proxy: %s', _redact_proxy_url(proxy_url) if proxy_url else 'disabled')

    request = HTTPXRequest(**req_kwargs)
    get_updates_request = HTTPXRequest(**get_updates_kwargs)

    app = (
        Application.builder()
        .token(token)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(_post_init)
        .build()
    )
    app.bot_data['svc'] = _service

    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('app', cmd_app))
    app.add_handler(CommandHandler('hunt', cmd_hunt))
    app.add_handler(CommandHandler('status', cmd_status))
    app.add_handler(CommandHandler('refresh', cmd_refresh))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text_button))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)

    logger.info('Telegram bot polling starting...')
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except RuntimeError:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
