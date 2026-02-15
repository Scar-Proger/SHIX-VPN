import pandas as pd
import json

# 1️⃣ Загружаем JSON из файла
with open("device.json", "r", encoding="utf-8") as f:
    data = json.load(f)  # JSON должен быть списком словарей

# 2️⃣ Создаем DataFrame (таблицу)
df = pd.DataFrame(data)

# 3️⃣ Проверяем первые строки, чтобы убедиться, что всё правильно
print(df.head())

# 4️⃣ Сохраняем в Excel с колонками
df.to_excel("device.xlsx", index=False)

print("Файл device.xlsx успешно создан с колонками!")
