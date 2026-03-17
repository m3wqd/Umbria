## IR-диаграмма (хранение объектов и выдача “на руки”)

Ниже — модель данных (ER/IR) для учета:
- объектов с IRF-меткой,
- ячеек хранения,
- пользователей с меткой/пропуском,
- выдачи объектов “на руки” с временем выдачи.

### Диаграмма (Mermaid ER)

```mermaid
erDiagram
  USERS ||--o{ HANDOUTS : "получает"
  OBJECTS ||--o{ HANDOUTS : "выдается"
  CELLS ||--o{ OBJECTS : "содержит"

  USERS {
    bigint user_id PK
    string full_name
    string pass_tag UNIQUE  "метка/пропуск"
    string status           "active/blocked/etc"
    datetime created_at
  }

  CELLS {
    bigint cell_id PK
    string cell_code UNIQUE "код/номер ячейки"
    string zone             "опционально: зона/шкаф"
    string status           "active/disabled/etc"
    datetime created_at
  }

  OBJECTS {
    bigint object_id PK
    string irf_tag UNIQUE   "IRF-метка (str)"
    string name
    bigint cell_id FK NULL  "NULL => объект на руках"
    string state            "ok/damaged/lost/etc"
    datetime created_at
  }

  HANDOUTS {
    bigint handout_id PK
    bigint object_id FK
    bigint user_id FK
    datetime issued_at      "время выдачи"
    datetime returned_at NULL
    string note NULL
  }
```

### Сущности и поля.

#### `OBJECTS` — Объекты
- **`irf_tag` (string, UNIQUE, NOT NULL)**: IRF-метка объекта.
- **`cell_id` (FK, NULL)**: где находится объект.
  - `cell_id IS NULL` означает **объект на руках** (не в ячейке).
  - если `cell_id` заполнен — объект **в ячейке** `CELLS`.

#### `CELLS` — Ячейки
- **`cell_code` (string, UNIQUE, NOT NULL)**: код/номер ячейки.
- Список ячеек — это таблица `CELLS`.

#### `USERS` — Пользователи
- **`pass_tag` (string, UNIQUE, NOT NULL)**: метка/пропуск пользователя.

#### `HANDOUTS` — Выдачи “на руки” (отдельный список)
Это “отдельный список у кого объект на руках, с временем выдачи”.
- **`issued_at` (datetime, NOT NULL)**: время выдачи.
- **`returned_at` (datetime, NULL)**: время возврата (если вернули).

### Правила и ограничения (рекомендуемые)

- **Один объект — одна IRF-метка**: `OBJECTS.irf_tag` уникальна.
- **Один пользователь — один пропуск**: `USERS.pass_tag` уникален.
- **Объект либо в ячейке, либо на руках**:
  - `OBJECTS.cell_id IS NOT NULL` => объект в ячейке, активной выдачи быть не должно.
  - `OBJECTS.cell_id IS NULL` => объект на руках, должна существовать активная выдача.
- **Активная выдача** — запись в `HANDOUTS` с `returned_at IS NULL`.
  - Рекомендуемое ограничение: **не более одной активной выдачи на объект** (уникальность по `object_id` среди `returned_at IS NULL`).

### Представления (удобные выборки)

#### “Кто держит объект на руках сейчас”
Логика: активные выдачи (`returned_at IS NULL`).
- **JOIN** `HANDOUTS` => `USERS` => `OBJECTS`
- **Фильтр**: `HANDOUTS.returned_at IS NULL`

#### “Где находится объект”
- Если `OBJECTS.cell_id` заполнен — `CELLS.cell_code`.
- Если `OBJECTS.cell_id` пустой — смотреть активную выдачу в `HANDOUTS`.

