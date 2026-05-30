import json
import os
from typing import Dict, List, Optional, Set
from dataclasses import dataclass
from collections import defaultdict
import logging


logger = logging.getLogger(__name__)


@dataclass
class SkinData:
    """Полные данные о скине"""
    id: str
    name: str
    weapon: str
    pattern: str
    rarity: str
    min_float: float
    max_float: float
    collection: str = ""
    rarity_id: str = ""
    wears: List[str] = None


@dataclass
class CollectionData:
    """Данные о коллекции"""
    id: str
    name: str
    skins: List[SkinData]


class CS2Database:
    """База данных коллекций и скинов CS2 на основе локальных JSON файлов"""
    
    def __init__(self, collections_file: str = "collections.json", skins_file: str = "skins.json"):
        self.collections_file = collections_file
        self.skins_file = skins_file
        self.collections: Dict[str, CollectionData] = {}
        self.skins: Dict[str, SkinData] = {}  # skin_id -> skin_data
        self.skins_by_name: Dict[str, SkinData] = {}  # exact skin_name -> skin_data
        self.skin_to_collection: Dict[str, str] = {}  # skin_name -> collection_name
        
        # Маппинг rarity_id в простые названия
        self.rarity_mapping = {
            "rarity_common": "Consumer",
            "rarity_uncommon": "Industrial", 
            "rarity_rare_weapon": "Mil-Spec",
            "rarity_mythical": "Restricted",
            "rarity_legendary": "Classified",
            "rarity_ancient": "Covert",
            "rarity_ancient_weapon": "Covert",
            "rarity_immortal": "Extraordinary",
            # Добавляем маппинг для полных названий
            "Consumer Grade": "Consumer",
            "Industrial Grade": "Industrial",
            "Mil-Spec Grade": "Mil-Spec",
            "Restricted": "Restricted",
            "Classified": "Classified",
            "Covert": "Covert",
            "Extraordinary": "Extraordinary"
        }
        
        # Обратный маппинг для поиска
        self.reverse_rarity_mapping = {v: k for k, v in self.rarity_mapping.items()}
    
    def load_data(self) -> bool:
        """Загрузка данных из локальных JSON файлов"""
        try:
            # Загрузка коллекций
            if not os.path.exists(self.collections_file):
                logger.error("Файл %s не найден", str(self.collections_file))
                return False
                
            with open(self.collections_file, 'r', encoding='utf-8') as f:
                collections_data = json.load(f)
            
            # Загрузка скинов
            if not os.path.exists(self.skins_file):
                logger.error("Файл %s не найден", str(self.skins_file))
                return False
                
            with open(self.skins_file, 'r', encoding='utf-8') as f:
                skins_data = json.load(f)
            
            self._parse_data(collections_data, skins_data)
            logger.info("Загружено %s коллекций и %s скинов", int(len(self.collections)), int(len(self.skins)))
            return True
            
        except Exception as e:
            logger.error("Ошибка загрузки данных: %s", str(e))
            return False
    
    def _parse_data(self, collections_data: List[dict], skins_data: List[dict]):
        """Парсинг данных о коллекциях и скинах с фильтрацией только оружия"""
        self.collections.clear()
        self.skins.clear()
        self.skins_by_name.clear()
        self.skin_to_collection.clear()
        
        # Создаем маппинг ID скинов -> полные данные
        skin_id_to_data = {}
        for skin_info in skins_data:
            # Проверяем наличие необходимых полей
            if not skin_info.get("id") or not skin_info.get("name"):
                continue
            
            # Фильтруем только оружие (исключаем перчатки, наклейки, нашивки, чармы, граффити)
            category = skin_info.get("category", {})
            category_name = category.get("name", "").lower() if category else ""
            
            # Исключаем нежелательные категории
            excluded_categories = [
                "gloves", "stickers", "patches", "charms", "graffiti", 
                "musical kits", "agents", "keys", "cases", "tools"
            ]
            
            if any(excluded in category_name for excluded in excluded_categories):
                continue
            
            # Проверяем, что это оружие (weapon должен существовать и не быть пустым)
            weapon_data = skin_info.get("weapon", {})
            if not weapon_data or not weapon_data.get("name"):
                continue
            
            pattern_data = skin_info.get("pattern", {})
            rarity_data = skin_info.get("rarity", {})
            
            skin_data = SkinData(
                id=skin_info["id"],
                name=skin_info["name"],
                weapon=weapon_data.get("name", "") if weapon_data else "",
                pattern=pattern_data.get("name", "") if pattern_data else "",
                rarity=rarity_data.get("name", "") if rarity_data else "",
                min_float=skin_info.get("min_float", 0.0),
                max_float=skin_info.get("max_float", 1.0),
                rarity_id=rarity_data.get("id", "") if rarity_data else "",
                wears=[w.get('name') for w in (skin_info.get('wears') or []) if isinstance(w, dict) and w.get('name')],
            )
            if skin_data.wears is None:
                skin_data.wears = []
            self.skins[skin_data.id] = skin_data
            self.skins_by_name[skin_data.name] = skin_data
            skin_id_to_data[skin_data.id] = skin_data
        
        # Парсим коллекции с фильтрацией
        for collection_info in collections_data:
            collection_name = collection_info.get("name", "")
            
            # Фильтруем коллекции: только те, что содержат "Collection" или "Case"
            if not ("collection" in collection_name.lower() or "case" in collection_name.lower()):
                continue
            
            collection = CollectionData(
                id=collection_info["id"],
                name=collection_name,
                skins=[]
            )
            
            # Добавляем скины из поля contains
            for skin_ref in collection_info.get("contains", []):
                skin_id = skin_ref.get("id")
                if skin_id and skin_id in skin_id_to_data:
                    skin_data = skin_id_to_data[skin_id]
                    skin_data.collection = collection.name
                    collection.skins.append(skin_data)
                    self.skin_to_collection[skin_data.name] = collection.name
            
            # Добавляем коллекцию только если в ней есть скины
            if collection.skins:
                self.collections[collection.name] = collection
    
    def get_collection(self, name: str) -> Optional[CollectionData]:
        """Получить коллекцию по имени"""
        return self.collections.get(name)
    
    def get_skin_by_name(self, name: str) -> Optional[SkinData]:
        """Получить скин по имени"""
        n = str(name or '').strip()
        if not n:
            return None

        # Exact match (authoritative)
        skin = self.skins_by_name.get(n)
        if skin is not None:
            return skin

        # Case-insensitive exact match
        n_low = n.lower()
        for k, v in self.skins_by_name.items():
            if k.lower() == n_low:
                return v

        return None
    
    def get_skin_collection(self, skin_name: str) -> Optional[str]:
        """Получить название коллекции по имени скина"""
        return self.skin_to_collection.get(skin_name)
    
    def get_skins_by_rarity(self, rarity: str, collection_name: str = None) -> List[SkinData]:
        """Получить скины указанного грейда"""
        result = []
        
        if collection_name:
            collection = self.get_collection(collection_name)
            if collection:
                result = [skin for skin in collection.skins if self._normalize_rarity(skin.rarity) == rarity]
        else:
            result = [skin for skin in self.skins.values() if self._normalize_rarity(skin.rarity) == rarity]
        
        return result
    
    def _normalize_rarity(self, rarity_name: str) -> str:
        """Нормализация названия грейда"""
        return self.rarity_mapping.get(rarity_name, rarity_name)
    
    def get_higher_rarity_skins(self, collection_name: str, current_rarity: str) -> List[SkinData]:
        """Получить скины более высокого грейда в той же коллекции"""
        collection = self.get_collection(collection_name)
        if not collection:
            return []
        
        current_level = self._get_rarity_level(current_rarity)
        higher_skins = []
        
        for skin in collection.skins:
            skin_level = self._get_rarity_level(self._normalize_rarity(skin.rarity))
            if skin_level > current_level:
                higher_skins.append(skin)
        
        return higher_skins
    
    def _get_rarity_level(self, rarity: str) -> int:
        """Получить числовой уровень грейда"""
        levels = {
            "Consumer": 0,
            "Industrial": 1,
            "Mil-Spec": 2,
            "Restricted": 3,
            "Classified": 4,
            "Covert": 5
        }
        return levels.get(rarity, 0)
    
    def get_collection_skins(self, collection_name: str) -> List[SkinData]:
        """Получение скинов конкретной коллекции"""
        if collection_name in self.collections:
            return self.collections[collection_name].skins
        return []
    
    def list_collections(self) -> List[str]:
        """Получить список всех коллекций"""
        return list(self.collections.keys())
    
    def get_collections_with_rarity(self, rarity: str) -> List[str]:
        """Получить коллекции, содержащие скины указанного грейда"""
        result = []
        for collection_name, collection in self.collections.items():
            has_rarity = any(self._normalize_rarity(skin.rarity) == rarity for skin in collection.skins)
            if has_rarity:
                result.append(collection_name)
        return result
    
    def get_skin_float_info(self, skin_name: str) -> Optional[tuple]:
        """Получить информацию о float для скина"""
        skin = self.get_skin_by_name(skin_name)
        if skin:
            return (skin.min_float, skin.max_float)
        return None
    
    def get_available_wears(self, skin_name: str) -> List[str]:
        """
        Get available wear levels for a skin.
        
        Args:
            skin_name: Exact skin name (e.g., "MAC-10 | Sakkaku")
            
        Returns:
            List of available wear levels. Empty list if skin not found.
        """
        skin = self.get_skin_by_name(skin_name)
        if not skin:
            return []
        
        if skin.wears:
            return skin.wears
        
        available = []
        if skin.min_float < 0.07:
            available.append('Factory New')
        if skin.min_float < 0.15 and skin.max_float >= 0.07:
            available.append('Minimal Wear')
        if skin.min_float < 0.38 and skin.max_float >= 0.15:
            available.append('Field-Tested')
        if skin.min_float < 0.45 and skin.max_float >= 0.38:
            available.append('Well-Worn')
        if skin.max_float >= 0.45:
            available.append('Battle-Scarred')
        
        return available if available else ['Factory New', 'Minimal Wear', 'Field-Tested', 'Well-Worn', 'Battle-Scarred']
    
    def calculate_max_average_float_for_fn(self, target_skin_name: str) -> float:
        """
        Рассчитать максимальный средний float для получения Factory New на основе реальных данных
        
        Args:
            target_skin_name: имя целевого скина
            
        Returns:
            Максимальный средний float для получения Factory New
        """
        skin = self.get_skin_by_name(target_skin_name)
        if not skin:
            return 0.07  # значение по умолчанию
        
        min_float, max_float = skin.min_float, skin.max_float
        
        # Для Factory New нужен float < 0.07 (эксклюзивная граница)
        # Но учитываем реальные границы скина
        fn_threshold = 0.07
        
        # Если скин не может быть Factory New (min_float >= 0.07)
        if min_float >= fn_threshold:
            return min_float  # возвращаем минимально возможный
        
        # Если скин всегда будет Factory New (max_float < 0.07)
        if max_float < fn_threshold:
            return max_float
        
        # В остальных случаях - порог для FN
        return fn_threshold
    
    def get_float_info_for_skin(self, skin_name: str) -> Optional[dict]:
        """
        Получить полную информацию о float для скина
        
        Returns:
            dict с min_float, max_float, can_be_fn, max_avg_float_for_fn
        """
        skin = self.get_skin_by_name(skin_name)
        if not skin:
            return None
        
        min_float, max_float = skin.min_float, skin.max_float
        fn_threshold = 0.07
        can_be_fn = min_float < fn_threshold
        max_avg_float = self.calculate_max_average_float_for_fn(skin_name)
        
        return {
            'min_float': min_float,
            'max_float': max_float,
            'fn_threshold': fn_threshold,
            'can_be_fn': can_be_fn,
            'max_avg_float_for_fn': max_avg_float
        }
    
    def debug_info(self):
        """Вывод отладочной информации"""
        print(f"Загружено коллекций: {len(self.collections)}")
        print(f"Загружено скинов: {len(self.skins)}")
        
        # Статистика по грейдам
        rarity_stats = defaultdict(int)
        for skin in self.skins.values():
            rarity = self._normalize_rarity(skin.rarity)
            rarity_stats[rarity] += 1
        
        print("Распределение по грейдам:")
        for rarity, count in sorted(rarity_stats.items()):
            print(f"  {rarity}: {count}")
        
        # Коллекции с Mil-Spec
        milspec_collections = self.get_collections_with_rarity("Mil-Spec")
        print(f"Коллекций с Mil-Spec: {len(milspec_collections)}")
        for collection in milspec_collections[:5]:  # первые 5
            milspec_count = len(self.get_skins_by_rarity("Mil-Spec", collection))
            print(f"  {collection}: {milspec_count} Mil-Spec")
