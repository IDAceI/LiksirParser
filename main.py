# === main.py ===
# Полный обход каталога Liksir.ru (ИСПРАВЛЕН БАГ с "мл" и "л")

import csv
import time
import random
import re
from curl_cffi import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from loguru import logger


# === НАСТРОЙКИ СКОРОСТИ ===
MIN_DELAY = 0.005
MAX_DELAY = 0.001

logger.add("parser.log", rotation="5 MB", encoding="utf-8")

class LiksirParser:
    BASE_URL = "https://liksir.ru"
    
    def __init__(self):
        self.session = requests.Session(impersonate="chrome124")
        self.session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        self.results = []
        self.visited = set()
        self.category_count = 0

    def get_subcategories(self, url: str) -> list:
        try:
            response = self.session.get(url, timeout=15)
            soup = BeautifulSoup(response.text, "html.parser")
            subcats = []
            cat_list = soup.find("div", class_="catalog_section_list")
            if cat_list:
                links = cat_list.find_all("a", class_="dark_link")
                for link in links:
                    href = link.get("href", "")
                    name_span = link.find("span", class_="font_md")
                    name = name_span.text.strip() if name_span else link.text.strip()
                    if href and name:
                        subcats.append({"name": name, "url": urljoin(self.BASE_URL, href)})
            return subcats
        except Exception as e:
            logger.error(f"Ошибка подкатегорий {url}: {e}")
            return []

    def get_product_details(self, product_url: str) -> dict:
        try:
            response = self.session.get(product_url, timeout=15)
            soup = BeautifulSoup(response.text, "html.parser")
            
            raw_pack_names = []
            variant_list = soup.find("ul", class_="list_values_wrapper")
            if variant_list:
                for item in variant_list.find_all("li", class_="item"):
                    name = item.get("title", "").replace("Фасовка: ", "")
                    if not name:
                        cnt_span = item.find("span", class_="cnt")
                        name = cnt_span.text.strip() if cnt_span else "Не указана"
                    if name and name not in raw_pack_names:
                        raw_pack_names.append(name)
            
                        #  ПРОСТОЙ КОСТЫЛЬ: смотрим на окончание строки после цифр
            pack_with_size = []
            for name in raw_pack_names:
                numbers = re.findall(r'\d+(?:\.\d+)?', name)
                
                if not numbers:
                    size_val = 999999999
                else:
                    val = float(numbers[0])
                    # Убираем все цифры и точки, смотрим что осталось (единица измерения)
                    unit = re.sub(r'[\d.\s]+', '', name).lower().strip()
                    
                    # мл, мл, гр, г — НЕ умножаем
                    if unit in ['мл', 'ml', 'гр', 'г', 'g', 'грамм', 'граммов', 'грам']:
                        size_val = val
                        logger.debug(f"   📦 {name} → единица '{unit}' → {size_val} (оставили)")
                    
                    # л, кг — умножаем на 1000
                    elif unit in ['л', 'l', 'литр', 'литров', 'кг', 'kg', 'килограмм', 'килограммов']:
                        size_val = val * 1000
                        logger.debug(f"   📦 {name} → единица '{unit}' → {size_val} (×1000)")
                    
                    else:
                        size_val = val
                        logger.debug(f"   📦 {name} → единица '{unit}' → {size_val} (неизвестно, оставили)")
                
                pack_with_size.append((size_val, name))
            
            logger.debug(f"   📋 ДО сортировки: {pack_with_size}")
            pack_with_size.sort(key=lambda x: x[0])
            sorted_pack_names = [item[1] for item in pack_with_size]
            logger.debug(f"   ✅ ПОСЛЕ сортировки: {sorted_pack_names}")

            # Собираем цены
            unique_prices = set()
            for meta in soup.find_all("meta", itemprop="price"):
                val = meta.get("content", "").strip()
                if val and val != "0" and val != "":
                    try:
                        unique_prices.add(float(val))
                    except ValueError:
                        pass
            
            sorted_prices = sorted(list(unique_prices))
            logger.debug(f"   💰 Цены: {list(unique_prices)} → отсортированы: {sorted_prices}")
            
            # Соединяем
            packaging_variants = []
            for i in range(max(len(sorted_pack_names), len(sorted_prices))):
                name = sorted_pack_names[i] if i < len(sorted_pack_names) else f"Вариант {i+1}"
                if i < len(sorted_prices):
                    price_val = sorted_prices[i]
                    formatted_price = f"{int(price_val):,} ₽".replace(",", " ")
                else:
                    formatted_price = "Под заказ"
                
                packaging_variants.append({"name": name, "price": formatted_price})
            
            if not packaging_variants:
                packaging_variants.append({"name": "Не указана", "price": "Под заказ"})
            
            stock_span = soup.find("span", class_="store_view")
            stock = stock_span.text.strip() if stock_span else "Не указано"
            
            is_preorder = soup.find("span", string=lambda t: t and "под заказ" in t.lower())
            status = "Под заказ" if is_preorder else "В наличии"
            
            return {"variants": packaging_variants, "stock": stock, "status": status}
            
        except Exception as e:
            logger.error(f"Ошибка парсинга товара {product_url}: {e}")
            return {"variants": [{"name": "Ошибка", "price": "Под заказ"}], "stock": "Ошибка", "status": "Ошибка"}

    def get_products(self, url: str) -> list:
        try:
            response = self.session.get(url, timeout=15)
            soup = BeautifulSoup(response.text, "html.parser")
            products = []
            
            cards = soup.find_all("div", class_=lambda x: x and "inner_wrap" in x and "TYPE_1" in x)
            
            for card in cards:
                title_a = card.select_one(".item-title .dark_link")
                if not title_a:
                    continue
                
                name = title_a.text.strip()
                link = urljoin(self.BASE_URL, title_a.get("href", ""))
                
                logger.info(f"   🔍 Заходим на страницу: {name[:30]}...")
                details = self.get_product_details(link)
                
                variants_str = " | ".join([f"{v['name']}: {v['price']}" for v in details["variants"]])
                availability = f"{details['stock']} ({details['status']})"
                
                products.append({
                    "Название": name,
                    "Варианты": variants_str,
                    "Доступность": availability,
                    "Ссылка": link
                })
                
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
            
            return products
        except Exception as e:
            logger.error(f"Ошибка парсинга товаров {url}: {e}")
            return []

    def crawl(self, url: str, path: str, depth: int = 0):
        if url in self.visited or depth > 9:
            return
        self.visited.add(url)
        
        indent = "  " * depth
        logger.info(f"{indent}📂 [{depth}] {path}")
        
        subcats = self.get_subcategories(url)
        
        if subcats and depth < 9:
            logger.info(f"{indent}   Найдено {len(subcats)} подкатегорий.")
            for sub in subcats:
                new_path = f"{path} > {sub['name']}" if path != "Каталог" else sub['name']
                self.crawl(sub['url'], new_path, depth + 1)
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        else:
            self.category_count += 1
            logger.info(f"{indent}   🛒 Парсим 1 товар из категории #{self.category_count}...")
            products = self.get_products(url)
            
            for p in products:
                p["Путь категории"] = path
                self.results.append(p)
                logger.success(f"{indent}   ✅ {p['Название'][:30]}...")
            
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    def save_to_csv(self):
        if not self.results:
            logger.warning("Нет данных для сохранения")
            return
        
        fieldnames = ["Путь категории", "Название", "Варианты", "Доступность", "Ссылка"]
        
        with open("liksir_full_catalog.csv", "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=';')
            writer.writeheader()
            writer.writerows(self.results)
            
        logger.success(f"💾 УСПЕХ! Сохранено {len(self.results)} товаров из {self.category_count} категорий в 'liksir_full_catalog.csv'")
        print("\n" + "="*70)
        print(f"✅ ПОЛНЫЙ ОБХОД ЗАВЕРШЕН!")
        print(f" Всего категорий: {self.category_count}")
        print(f"📦 Всего товаров: {len(self.results)}")
        print(f" Открой файл 'liksir_full_catalog.csv' двойным кликом!")
        print("="*70)

    def run(self):
        print("=" * 70)
        print(" 🚀 ПОЛНЫЙ ОБХОД КАТАЛОГА LIKSIR.RU (БАГ С 'мл' ИСПРАВЛЕН)")
        print("=" * 70)
        self.crawl(f"{self.BASE_URL}/catalog/", "Каталог", depth=0)
        self.save_to_csv()

if __name__ == "__main__":
    parser = LiksirParser()
    parser.run()