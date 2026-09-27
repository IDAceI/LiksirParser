# === clean.py ===
# Быстрая очистка CSV от дубликатов

import pandas as pd

# 1. Читаем наш файл (разделитель ; и кодировка utf-8-sig для Excel)
df = pd.read_csv("liksir_full_catalog.csv", sep=';', encoding='utf-8-sig')

print(f"📊 Было строк в файле: {len(df)}")

# 2. Удаляем дубликаты по столбцу "Ссылка" 
# (если ссылка одинаковая — значит это один и тот же товар)
df_clean = df.drop_duplicates(subset=['Ссылка'])

print(f"✅ Стало уникальных строк: {len(df_clean)}")
print(f"🗑️ Удалили дубликатов: {len(df) - len(df_clean)}")

# 3. Сохраняем в новый чистый файл
df_clean.to_csv("liksir_clean_catalog.csv", sep=';', index=False, encoding='utf-8-sig')

print("\n💾 Готово! Чистый файл сохранен как 'liksir_clean_catalog.csv'")