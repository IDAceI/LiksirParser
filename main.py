# === main.py ===
# Обход каталога Liksir.ru

import csv
import os
import random
import re
import time
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

from bs4 import BeautifulSoup
from curl_cffi import requests
from loguru import logger

# === НАСТРОЙКИ ===
TEST_MODE = False   # True = только тестовая категория, файлы с префиксом test_
TEST_CATEGORY_URL = "https://liksir.ru/catalog/maslo-industrialnoe/gidravlicheskoe_maslo/hfdr-masla/"

MIN_DELAY = 0.3
MAX_DELAY = 0.8
RETRIES = 3            # попыток на один запрос
MAX_PAGES = 200        # защита от бесконечной пагинации
MAX_DEPTH = 9

PREFIX = "test_" if TEST_MODE else ""
CSV_FILE = f"{PREFIX}liksir_full_catalog.csv"
DONE_FILE = f"{PREFIX}done_categories.txt"     # для возобновления после падения
FAILED_FILE = f"{PREFIX}failed_urls.txt"       # URL, которые не удалось загрузить
EMPTY_FILE = f"{PREFIX}empty_categories.txt"   # категории, где не нашлось карточек товаров
FIELDNAMES = ["Путь категории", "Название", "Варианты", "Доступность", "Ссылка"]

logger.add("parser.log", rotation="5 MB", encoding="utf-8")

# Множители к базовой единице (мл / г)
UNIT_MULT = {
    "мл": 1, "ml": 1,
    "г": 1, "гр": 1, "g": 1, "грамм": 1, "граммов": 1, "грам": 1,
    "л": 1000, "l": 1000, "литр": 1000, "литров": 1000,
    "кг": 1000, "kg": 1000, "килограмм": 1000, "килограммов": 1000,
}


def pause():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def pack_size(name: str) -> float:
    """Размер фасовки в мл/г. Понимает '0,5 л', '50 мл (тестер)', '1.5 кг'."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*([a-zA-Zа-яА-ЯёЁ]+)", name)
    if not m:
        return float("inf")
    val = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    mult = UNIT_MULT.get(unit)
    if mult is None:
        logger.debug(f"   📦 {name} → неизвестная единица '{unit}', оставили как есть")
        mult = 1
    return val * mult


def read_lines(path: str) -> set:
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def append_line(path: str, line: str):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


class LiksirParser:
    BASE_URL = "https://liksir.ru"

    def __init__(self):
        self.session = requests.Session(impersonate="chrome124")
        # User-Agent не задаём: impersonate ставит согласованный сам
        self.session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9",
        })
        self.visited = set()
        self.category_count = 0
        self.total_products = 0
        self.empty_streak = 0

        # Возобновление: что уже сохранено
        self.done_categories = read_lines(DONE_FILE)
        self.seen_links = self._load_seen_links()
        if self.done_categories:
            logger.info(f"♻️ Возобновление: готово категорий {len(self.done_categories)}, "
                        f"товаров в CSV {len(self.seen_links)}")

    # ---------- служебное ----------

    def _load_seen_links(self) -> set:
        links = set()
        if os.path.exists(CSV_FILE):
            try:
                with open(CSV_FILE, encoding="utf-8-sig", newline="") as f:
                    for row in csv.DictReader(f, delimiter=";"):
                        if row.get("Ссылка"):
                            links.add(row["Ссылка"])
            except Exception as e:
                logger.error(f"Не удалось прочитать {CSV_FILE}: {e}")
        return links

    def fetch(self, url: str):
        """GET с ретраями и проверкой статуса. Возвращает response или None."""
        for attempt in range(1, RETRIES + 1):
            try:
                response = self.session.get(url, timeout=15)
                code = response.status_code
                if code == 200:
                    return response
                if code == 404:
                    logger.warning(f"404: {url}")
                    break
                logger.warning(f"HTTP {code} для {url} (попытка {attempt}/{RETRIES})")
                # при 403/429/5xx ждём дольше
                time.sleep(3 * attempt)
            except Exception as e:
                logger.warning(f"Ошибка запроса {url} (попытка {attempt}/{RETRIES}): {e}")
                time.sleep(2 * attempt)
        logger.error(f"❌ Не удалось загрузить: {url}")
        append_line(FAILED_FILE, url)
        return None

    @staticmethod
    def is_likely_blocked(soup) -> bool:
        """Страница с кодом 200, но на деле заглушка блокировки (определяем по заголовку)."""
        title = soup.title.get_text(strip=True).lower() if soup.title else ""
        return "доступ ограничен" in title or ("внимание" in title and "бот" in title)

    @staticmethod
    def find_cards(soup) -> list:
        """Карточки товаров в обеих вёрстках: плиточной и списочной."""
        tile = soup.find_all("div", class_=lambda x: x and "inner_wrap" in x and "TYPE_1" in x)
        lst = soup.select("div.list_item.item_info")
        return list(tile) + list(lst)

    @staticmethod
    def with_page(url: str, param: str, page: int) -> str:
        parts = urlparse(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query[param] = str(page)
        return urlunparse(parts._replace(query=urlencode(query)))

    @staticmethod
    def find_page_param(soup):
        """Ищем имя параметра пагинации (PAGEN_1, PAGEN_2 ...) в ссылках."""
        for a in soup.find_all("a", href=True):
            m = re.search(r"(PAGEN_\d+)=\d+", a["href"])
            if m:
                return m.group(1)
        return None

    # ---------- парсинг ----------

    def get_subcategories(self, url: str):
        """Список подкатегорий или None, если страницу загрузить не удалось."""
        response = self.fetch(url)
        if response is None:
            return None
        try:
            soup = BeautifulSoup(response.text, "html.parser")
            if self.is_likely_blocked(soup):
                logger.warning(f"Пропускаем {url}: страница-заглушка (возможно, блок)")
                append_line(FAILED_FILE, url)
                return None
            subcats = []
            cat_list = soup.find("div", class_="catalog_section_list")
            if cat_list:
                for link in cat_list.find_all("a", class_="dark_link"):
                    href = link.get("href", "")
                    name_span = link.find("span", class_="font_md")
                    name = name_span.text.strip() if name_span else link.text.strip()
                    if href and name:
                        subcats.append({"name": name, "url": urljoin(self.BASE_URL, href)})
            return subcats
        except Exception as e:
            logger.error(f"Ошибка подкатегорий {url}: {e}")
            return None

    def get_product_details(self, product_url: str) -> dict:
        error_result = {"variants": [{"name": "Ошибка", "price": "Под заказ"}],
                        "stock": "Ошибка", "status": "Ошибка"}
        response = self.fetch(product_url)
        if response is None:
            return error_result
        try:
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

            # Сортировка фасовок по реальному размеру (мл/г)
            sorted_pack_names = sorted(raw_pack_names, key=pack_size)
            logger.debug(f"   ✅ Фасовки: {raw_pack_names} → {sorted_pack_names}")

            # Цены
            unique_prices = set()
            for meta in soup.find_all("meta", itemprop="price"):
                val = meta.get("content", "").strip().replace(",", ".")
                if val and val != "0":
                    try:
                        unique_prices.add(float(val))
                    except ValueError:
                        pass
            sorted_prices = sorted(unique_prices)
            logger.debug(f"   💰 Цены: {sorted_prices}")

            # Соединяем (по порядку возрастания)
            packaging_variants = []
            for i in range(max(len(sorted_pack_names), len(sorted_prices))):
                name = sorted_pack_names[i] if i < len(sorted_pack_names) else f"Вариант {i + 1}"
                if i < len(sorted_prices):
                    formatted_price = f"{int(sorted_prices[i]):,} ₽".replace(",", " ")
                else:
                    formatted_price = "Под заказ"
                packaging_variants.append({"name": name, "price": formatted_price})

            if not packaging_variants:
                packaging_variants.append({"name": "Не указана", "price": "Под заказ"})

            # Наличие и статус
            stock_span = soup.find("span", class_="store_view")
            stock = stock_span.text.strip() if stock_span else "Не указано"

            cart_block = soup.find("div", class_="catalog_detail_redesign__cart-actions")
            if cart_block:
                is_preorder = "под заказ" in cart_block.get_text().lower()
                status = "Под заказ" if is_preorder else "В наличии"
            elif stock_span:
                is_preorder = "под заказ" in stock_span.get_text().lower()
                status = "Под заказ" if is_preorder else "В наличии"
            else:
                status = "Не определено"

            return {"variants": packaging_variants, "stock": stock, "status": status}

        except Exception as e:
            logger.error(f"Ошибка парсинга товара {product_url}: {e}")
            return error_result

    def get_products(self, url: str):
        """Все товары категории с учётом пагинации.
        Возвращает (список товаров, ok). ok=False — категорию нельзя считать завершённой."""
        products = []
        seen_on_this_category = set()
        page_param = None
        page = 1

        while page <= MAX_PAGES:
            page_url = url if page == 1 else self.with_page(url, page_param, page)
            response = self.fetch(page_url)
            if response is None:
                # упала не первая страница — сохраняем что есть, но категорию не закрываем
                return products, False

            soup = BeautifulSoup(response.text, "html.parser")

            if self.is_likely_blocked(soup):
                logger.warning(f"Страница-заглушка на {page_url}, категория не закрыта")
                append_line(FAILED_FILE, page_url)
                return products, False

            if page == 1:
                page_param = self.find_page_param(soup)

            cards = self.find_cards(soup)

            if page == 1:
                if not cards:
                    # Не считаем категорию готовой: вдруг это новая вёрстка, которую мы не знаем
                    self.empty_streak += 1
                    logger.warning(f"   ⚠️ Карточек товаров не найдено: {page_url} "
                                   f"(таких подряд: {self.empty_streak}). Категория не закрыта.")
                    append_line(EMPTY_FILE, page_url)
                    if self.empty_streak >= 10:
                        logger.error("⚠️ 10 категорий подряд без карточек — похоже на блокировку. Пауза 60 с.")
                        time.sleep(60)
                    return [], False
                self.empty_streak = 0

            new_on_page = 0
            for card in cards:
                title_a = card.select_one(".item-title .dark_link")
                if not title_a:
                    continue

                name = title_a.text.strip()
                link = urljoin(self.BASE_URL, title_a.get("href", ""))

                if link in seen_on_this_category:
                    continue
                seen_on_this_category.add(link)
                new_on_page += 1

                # товар уже собран в другой категории — пропускаем
                if link in self.seen_links:
                    logger.debug(f"   ⏭️ Дубль, пропускаем: {name[:30]}")
                    continue

                logger.info(f"   🔍 Заходим на страницу: {name[:30]}...")
                details = self.get_product_details(link)

                variants_str = " | ".join(f"{v['name']}: {v['price']}" for v in details["variants"])
                availability = f"{details['stock']} ({details['status']})"

                products.append({
                    "Название": name,
                    "Варианты": variants_str,
                    "Доступность": availability,
                    "Ссылка": link,
                })
                self.seen_links.add(link)
                pause()

            # нет новых товаров или нет пагинации — это последняя страница
            if new_on_page == 0 or not page_param:
                break
            logger.info(f"   📄 Страница {page} обработана, идём дальше...")
            page += 1
            pause()

        return products, True

    # ---------- сохранение ----------

    def append_to_csv(self, rows: list):
        if not rows:
            return
        write_header = not os.path.exists(CSV_FILE) or os.path.getsize(CSV_FILE) == 0
        with open(CSV_FILE, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES, delimiter=";")
            if write_header:
                writer.writeheader()
            writer.writerows(rows)

    # ---------- обход ----------

    def crawl(self, url: str, path: str, depth: int = 0):
        if url in self.visited or depth > MAX_DEPTH:
            return
        self.visited.add(url)

        indent = "  " * depth
        logger.info(f"{indent}📂 [{depth}] {path}")

        subcats = self.get_subcategories(url)
        if subcats is None:
            logger.error(f"{indent}   Пропускаем категорию (не загрузилась): {path}")
            return

        if subcats and depth < MAX_DEPTH:
            logger.info(f"{indent}   Найдено {len(subcats)} подкатегорий.")
            for sub in subcats:
                new_path = f"{path} > {sub['name']}" if path != "Каталог" else sub["name"]
                self.crawl(sub["url"], new_path, depth + 1)
                pause()
            return

        # листовая категория
        if url in self.done_categories:
            logger.info(f"{indent}   ⏭️ Уже обработана ранее, пропускаем")
            return

        self.category_count += 1
        logger.info(f"{indent}   🛒 Парсим товары категории #{self.category_count}...")
        products, ok = self.get_products(url)

        for p in products:
            p["Путь категории"] = path
            logger.success(f"{indent}   ✅ {p['Название'][:30]}...")

        self.append_to_csv(products)
        self.total_products += len(products)

        if ok:
            append_line(DONE_FILE, url)
            self.done_categories.add(url)
        pause()

    def finish(self):
        logger.success(f"💾 Готово! Новых товаров: {self.total_products}, "
                       f"категорий обработано: {self.category_count}. Файл: '{CSV_FILE}'")
        print("\n" + "=" * 70)
        print("✅ ОБХОД ЗАВЕРШЕН!")
        print(f"   Категорий в этом запуске: {self.category_count}")
        print(f"📦 Новых товаров: {self.total_products}")
        print(f"📄 Всего в файле: {len(self.seen_links)}")
        if os.path.exists(FAILED_FILE):
            print(f"⚠️ Часть URL не загрузилась, см. '{FAILED_FILE}'")
        if os.path.exists(EMPTY_FILE):
            print(f"⚠️ В части категорий не найдены карточки, см. '{EMPTY_FILE}'")
        print(f"   Открой файл '{CSV_FILE}' двойным кликом!")
        print("=" * 70)

    def run(self):
        print("=" * 70)
        if TEST_MODE:
            print(" 🧪 ТЕСТОВЫЙ РЕЖИМ: одна категория (TEST_MODE = True)")
            start_url, start_path = TEST_CATEGORY_URL, "Тестовая категория"
        else:
            print(" 🚀 ПОЛНЫЙ ОБХОД КАТАЛОГА LIKSIR.RU")
            start_url, start_path = f"{self.BASE_URL}/catalog/", "Каталог"
        print("=" * 70)
        try:
            self.crawl(start_url, start_path, depth=0)
        except KeyboardInterrupt:
            logger.warning("Остановлено пользователем. Данные сохранены, "
                           "при следующем запуске обход продолжится.")
        self.finish()


if __name__ == "__main__":
    LiksirParser().run()