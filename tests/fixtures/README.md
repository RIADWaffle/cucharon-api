# Fixtures

Respuestas JSON **reales** de las APIs externas, capturadas una vez y recortadas. Las
usan los unit tests de parsing de cada provider (con `httpx.MockTransport`).

Capturadas el 2026-10-15 con el User-Agent de la app:

- `off/`: Open Food Facts, `GET /api/v3.4/product/{code}` y `GET /cgi/search.pl`.
  - Recorte: solo los nutrientes que usamos (`<n>_100g` y `<n>_unit`), las primeras 4
    categorías y sin `nutriments_estimated` (valores estimados por OFF, no de etiqueta).
  - `product_kj_only.json` es `product_solid.json` sin los campos `energy-kcal*`
    (OFF casi siempre trae ambos; el caso existe en productos con datos parciales).
- `usda/`: USDA FoodData Central, `POST /fdc/v1/foods/search`.
  - Recorte: campos del alimento que usamos y solo los nutrientes con id conocido
    (más calcio, para probar que el resto se ignora).

Si una API cambia de forma, recapturar (no editar a mano salvo para recortar) y
actualizar los contract tests (`tests/contract`, `-m external`).
