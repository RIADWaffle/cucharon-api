# 0006. Products y food data: OFF + USDA, caché, búsqueda en español y datos nutrimentales

- Estado: aceptado
- Fecha: 2026-10-15

## Contexto

`products` necesita escaneo por código de barras, búsqueda por texto en español y productos custom (`MODULES.md`). Los datos vienen de Open Food Facts (OFF, productos con marca) y USDA FoodData Central (alimentos genéricos). Las dos APIs tienen límites estrictos, datos incompletos y formatos distintos, y el backend en Render free sale a internet desde **una sola IP** compartida por todos los usuarios.

## Lo verificado (2026-10-15)

Documentación oficial más requests reales con nuestro User-Agent. "Observado" = visto en respuestas reales, no documentado.

### Open Food Facts
- La API v3 es la recomendada; la v2 está deprecada. Desde **v3.5** cambió la estructura de nutrientes: "New product nutrition facts structure… still currently under active development" ([change log](https://openfoodfacts.github.io/openfoodfacts-server/api/ref-api-and-product-schema-change-log/)).
  - Observado: con `/api/v3.6/` el objeto `nutriments` llega vacío; con `/api/v3/`, v3.3 y v3.4 llega completo.
  - **Fijamos `/api/v3.4/product/{code}`**.
- El "no encontrado" en v3 es HTTP 404 con `status: "failure"` y `result.id: "product_not_found"` (observado).
- **Búsqueda de texto:** "Full-text search is **not** available in the v2 server-side API" ([docs](https://openfoodfacts.github.io/openfoodfacts-server/api/)). Solo existe el endpoint legado `/cgi/search.pl` (`search_terms`, `search_simple=1`, `action=process`, `json=1`, `page_size`, `fields`, `lc`).
  - Search-a-licious "will be available": no es oficial todavía.
  - Observado: `search.pl` responde **503 con frecuencia** cuando OFF está saturado.
- **Límites:** "15 req/min/IP" para lecturas de producto y "10 req/min/IP" para búsquedas, "don't use it for a search-as-you-type feature". Si se exceden: "we reserve the right to deny you access… through IP address ban" ([docs](https://openfoodfacts.github.io/openfoodfacts-server/api/)).
- **User-Agent:** se exige con la forma `AppName/Version (ContactEmail)`.
- **Normalización de barcodes del lado de OFF:** rellena 9–12 dígitos a 13 y ≤7 a 8 ([barcode normalization](https://openfoodfacts.github.io/openfoodfacts-server/api/ref-barcode-normalization/)).
- **Nutrientes:** `<n>_100g` es "the normalized value… for 100g (or 100ml for liquids)", en unidades estándar; el sodio va en **gramos**. `energy_100g` es kJ. `carbohydrates` son carbohidratos **disponibles** (sin fibra).
  - Observado: `nutrition_data_per` también vale `"100ml"` aunque la doc solo lista `100g|serving`.
- **Licencia** ([terms of use](https://world.openfoodfacts.org/terms-of-use), [data](https://world.openfoodfacts.org/data)): la base está bajo ODbL, los contenidos bajo DbCL y las imágenes bajo CC BY-SA.
  - Se debe "mention the licence and… attribute the authorship to Open Food Facts with a link… or the product page".
  - La atribución va en la UI (producto, búsqueda, "Acerca de") con enlaces a OFF y a la ODbL.
  - Share-alike si se usa públicamente una versión adaptada de la base.
  - OFF pide no mezclar sus datos con otros y devolverle las correcciones.

### USDA FoodData Central
- **Endpoint y auth:** `POST /fdc/v1/foods/search` con `query`, `dataType` (`Foundation`, `SR Legacy`, `Survey (FNDDS)`, `Branded`) y `pageSize` ([api guide](https://fdc.nal.usda.gov/api-guide/), spec YAML de la API). La key va en el header `X-Api-Key`, que documenta [api.data.gov](https://api.data.gov/docs/developer-manual/) y verificamos.
- Observado: **`GET /foods/search` regresa 400 intermitentes desde nginx** (HTML) para la misma request válida: 4 de 6 intentos. El POST con JSON respondió 6 de 6. **Usamos POST.**
- **Límites:** "1,000 requests per hour"; si se exceden, la key "temporarily blocked for 1 hour" y la API responde 429. `DEMO_KEY` permite 30 por hora y 50 por día por IP.
  - Observado: nuestra key reporta `X-RateLimit-Limit: 3600`. Nos quedamos con el número documentado.
- **Base de los valores:**
  - Foundation, SR Legacy y FNDDS van por 100 g.
  - Branded: "converted to a 100-unit basis, either gram (g) or milliliter (ml)" ([GBFPD](https://fdc.nal.usda.gov/GBFPD_Documentation/)). No hay campo para la unidad. Observado: `servingSizeUnit` llega como `MLT` o `ml` en bebidas, y los valores son por 100 ml.
- **Energía y carbohidratos:**
  - Foundation ya no trae "Energy" 1008 ([Foundation docs](https://fdc.nal.usda.gov/Foundation_Foods_Documentation/)). Usamos 1008, luego 2048 (Atwater específico), luego 2047 (general), luego kJ 1062 convertido.
  - "Carbohydrate, by difference" (1005) **incluye** la fibra.
- **Otros ids usados (observados):** proteína 1003, grasa 1004, fibra 1079, azúcares 2000 (o 1063), sodio 1093 (mg).
- **Idioma:** USDA busca solo en inglés en la práctica. Observado: `query=manzana` regresa refrescos "APPLE MANZANA SODA".
- **Licencia:** dominio público / CC0. La cita es opcional.

### Postgres / Neon
- Neon soporta `pg_trgm` 1.6 y `unaccent` 1.1 en PG 14–18 ([extensiones de Neon](https://neon.com/docs/extensions/pg-extensions)). Ambas son *trusted*, así que se instalan sin superuser ([pg_trgm](https://www.postgresql.org/docs/current/pgtrgm.html), [unaccent](https://www.postgresql.org/docs/current/unaccent.html)).
- `unaccent()` es `STABLE`: depende de un diccionario configurable, así que no sirve en índices ni en columnas generadas.
  - Se usa un wrapper `IMMUTABLE` en SQL, con esquema y diccionario calificados (`public.unaccent('public.unaccent'::regdictionary, $1)`). Es la práctica estándar.
  - En SQL y no en C, porque Neon no da superuser.
  - PG 17 exige `search_path` seguro en funciones de índices, y la calificación lo cubre.

## Decisiones

### D1. Códigos de barras: GTIN-14 canónico
- Se aceptan UPC-A, EAN-8, EAN-13 y GTIN-14 con dígito verificador GS1 válido. Si no, `422 invalid_barcode` **sin llamar al provider**. Va en `products/domain.py::normalize_gtin`.
- Se guardan y buscan como GTIN-14 (14 dígitos con ceros a la izquierda), el único formato que representa todos sin perder información.
- A OFF se le manda con 8 dígitos si cabe, si no con 13 (`off.py::off_code`).
- UPC-E no se acepta: el cliente lo expande a UPC-A.

### D2. Solo OFF resuelve códigos
- USDA se usa para genéricos; su `get_by_barcode` regresa `None`. USDA Branded repite el mismo `gtinUpc` en varios `fdcId`, así que un unique por barcode tronaría.
- Invariante en la DB (CHECK `barcode_only_off`): `barcode IS NULL OR (source = 'off' AND external_id = barcode)`. Junto con unique `(source, external_id)`, esto **ya garantiza que `barcode` es único**: dos filas con el mismo código serían las dos `off` con el mismo `external_id`. Por eso `barcode` lleva un índice **no único** (`ix_products_barcode`), solo para buscar. Ver D5.

### D3. Caché positiva
- **Dentro de `PRODUCTS_CACHE_TTL_DAYS` (30):** se responde sin llamar al provider.
- **Vencida, se refresca en línea:**
  - Si el provider falla, se sirve la copia vieja con `stale: true`.
  - Si el provider dice "ya no existe", también se sirve la copia, se actualiza `fetched_at` y se loguea.
  - Nunca se borra: hay FKs a `products`.
- **Sin tareas en segundo plano:** Render free las mata y no se pueden probar de forma determinista.

### D4. Caché negativa
- Tabla `product_barcode_misses(barcode, checked_at)`, con TTL `PRODUCTS_MISS_TTL_HOURS` (72).
- **Solo se escribe** si **todos** los providers respondieron "no existe" sin fallar. Si alguno falló, la cadena lanza `ProviderUnavailableError` (y no `None`) justamente para esto.
- Un custom del usuario con ese código se revisa antes que la caché negativa.

### D5. Concurrencia
- **Barcode:** `INSERT … ON CONFLICT (source, external_id) DO UPDATE … RETURNING`.
- **Búsqueda:** `INSERT … ON CONFLICT DO NOTHING` (mismo árbitro) y después `SELECT … FOR UPDATE` de los que ya existían.
- **Un solo índice único en `products` además de la PK.** `ON CONFLICT` solo arbitra un índice (`uq_products_source_external_id`); cualquier otro índice único se verifica de forma normal. La primera versión tenía además `uq_products_barcode`. Dos escaneos simultáneos del mismo código podían pasar los dos el chequeo del árbitro y luego chocar en ese otro índice, con `UniqueViolationError`, según el timing (falló en CI). La migración `085adeb10b5e` lo cambió por un índice no único; la unicidad la sigue garantizando D2. La PK `id` es un `uuid4` nuevo en cada insert, así que no compite.
- **Regla:** no agregar a `products` otro índice único mientras se use `ON CONFLICT` sobre uno solo. La verifica de forma determinista `test_products_has_a_single_unique_index_besides_pk`, que lee `pg_indexes`. `product_barcode_misses` solo tiene la PK, que es su árbitro, y `user_products` no usa `ON CONFLICT` (inserta y traduce el `IntegrityError` a 409).
- **Test:** repite la carrera 200 veces, con 4 escaneos simultáneos por código y códigos distintos, porque el choque ocurre dentro de Postgres y no se puede forzar desde el cliente. Con el índice único viejo falló 5 de 5 corridas, y sin `ON CONFLICT` falla siempre.

### D6. La búsqueda nunca degrada la caché (`domain.py::search_upsert_action`, pura)
Cada fila guarda `fetched_via` (`barcode` | `search`). Al guardar un resultado de búsqueda sobre una fila existente:

| Situación | Acción |
|---|---|
| Trae exactamente lo mismo | `TOUCH`: solo `fetched_at` |
| La fila vino de un lookup por barcode (autoritativo) | `KEEP` |
| La fila tiene más de los 7 nutrientes principales | `KEEP` |
| Otro caso | `REPLACE` |

Los alias de búsqueda (glosario de USDA) se acumulan siempre. Test: escaneo completo → búsqueda con campos faltantes → el producto sigue completo y su `fetched_at` no cambia.

### D7. Orden del escaneo
1. Validar.
2. Override o custom del usuario.
3. Caché fresca.
4. Caché negativa fresca: 404.
5. Provider:
   - Encontrado: se guarda.
   - No existe: caché negativa y `404 product_not_found` (el cliente ofrece crear un custom).
   - Falla: copia vieja, o `503 food_data_unavailable` con `Retry-After`.

**Sin transacción abierta mientras se espera al provider.** Los pasos 1–4 solo leen. Antes del paso 5 el service hace `session.rollback()`, que no pierde nada (no hubo escrituras) y devuelve la conexión al pool. La llamada al provider (hasta 8 s) ocurre sin DB, y la escritura (upsert, caché negativa, `fetched_at`) va en una transacción nueva que commitea `get_session`.
- Así no quedan conexiones "idle in transaction" ni transacciones largas (ADR 0004), y el pool no se agota con escaneos lentos.
- **Precondición, verificada en código:** en esa transacción no hubo escrituras. Unos listeners de sesión en `core/db.py` (`after_flush`, y `do_orm_execute` para todo `session.execute` que no sea SELECT, incluido `text(...)`) marcan `session.info["has_writes"]`, que se limpia al terminar la transacción raíz. Si hubo escrituras o hay cambios pendientes (`new`/`dirty`/`deleted`), `_release_before_provider` lanza `RuntimeError`: es un error de programación (500), no de dominio. Es conservador: un `SELECT` escrito con `text(...)` también cuenta como escritura.
- Lo prueba un integration test con un fake que revisa `pool.checkedout() == 0` durante la llamada al provider, en escaneo y en búsqueda.

**El 404 se regresa como respuesta, no como excepción.** `lookup_barcode` regresa `None` y el router responde el 404 con `error_response`. Si se lanzara la excepción, `get_session` haría rollback y la caché negativa recién escrita se perdería. Esto pasaba en la primera versión y lo detectó un e2e: el segundo escaneo del mismo código inexistente volvía a llamar al provider.

### D8. Búsqueda en español, sin LLM
- **Normalización:** `search_text` es una columna generada, `immutable_unaccent(lower(nombre + marca + alias))`, con índice GIN `gin_trgm_ops`, en `products` y `user_products`. Coincide con `'consulta' <% search_text` (word_similarity ≥ 0.6) o `LIKE '%consulta%'`. "platano" encuentra "plátano".
- **Orden de fuentes:**
  1. Productos del usuario.
  2. Caché.
  3. OFF y USDA en paralelo, **solo si** hay menos coincidencias locales que `limit`, para ahorrar la cuota mínima de OFF. Igual que en el escaneo (D7), se cierra la transacción de lectura antes de llamarlos y se vuelve a leer después.
  
  Lo externo se guarda (D6) y se rankea junto con lo local, con un solo criterio.
- **USDA en español:** glosario curado ES→EN de unos 130 genéricos (`usda_glossary_es.json`), con coincidencia exacta o quitando un plural simple.
  - El nombre queda en inglés.
  - El término en español se guarda como alias, así que la siguiente búsqueda en español lo encuentra localmente.
  - Sin entrada en el glosario, la consulta va tal cual (sirve si viene en inglés).
- **Tipos de dato de USDA:** primero los genéricos (Foundation, SR Legacy, FNDDS). Branded solo si no hubo genéricos.
- **Ranking** (`domain.py::rank_candidates`, puro):
  1. Productos del usuario (sus overrides reemplazan al producto base).
  2. Coincidencia exacta, luego prefijo.
  3. Similitud (redondeada a 0.05).
  4. Datos completos y sin flags.
  5. Fuente: USDA Foundation > SR Legacy > FNDDS > OFF > USDA Branded.
  6. Nombre.
- **Respuesta:** `{items, partial}`; `partial` indica que una fuente falló o no tenía cuota.

### D9. Bebidas, base y densidad (impacta recipes y metrics)
- Todo nutriente es **por 100 de la base**: `basis = per_100g | per_100ml`.
- **Nunca se asume una densidad (ni 1 g/ml).** La única fuente de densidad es `user_products.density_g_per_ml`, puesta por el usuario; los providers no la traen.
- **Consecuencias para las fases siguientes:**
  - Recipes y metrics deben calcular en la unidad de la base del producto.
  - Las columnas `grams` de `recipe_ingredients` y `food_log_entries` en `MODULES.md` tendrán que pasar a cantidad + unidad (g o ml). Se decide en la fase de recipes.
  - "200 g de Pepsi" sin densidad queda como ingrediente **incompleto**: no se estima.
  - Las tazas y cucharadas convierten a ml y, si la base es g, necesitan densidad.
- **Cómo se decide la base:**
  - OFF: `per_100ml` si `nutrition_data_per = 100ml` o si la unidad de cantidad o porción es `ml`. Si no hay unidad y la categoría es bebida, se marca `basis_uncertain`.
  - USDA Branded: según `servingSizeUnit`.

### D10. Carbohidratos disponibles
- `carbs_g` son carbohidratos **disponibles (sin fibra)**: es lo que reporta OFF y lo que exige la **NOM-051** en las etiquetas mexicanas.
- En USDA se calcula "by difference" − fibra. Si falta la fibra, se deja "by difference" con el flag `carbs_may_include_fiber`.
- La energía estimada usa Atwater con disponibles: 4P + 4C + 9G + 2·fibra + 7·alcohol.

### D11. Normalización y calidad (`providers/food_data/normalize.py`)
- **Conversiones (no se inventa nada):**
  - kJ → kcal: ÷ 4.184.
  - Sodio de OFF: g → mg.
  - Sal → sodio: ÷ 2.5.
  - Lo que falta queda `None`.
- `nutrition_complete` = kcal, proteína, carbohidratos y grasa presentes.
- **`quality_flags`** se **marcan, no se corrigen**:
  - `negative_value`
  - `energy_out_of_range` (> 920 kcal)
  - `sodium_out_of_range` (> 40 000 mg)
  - `macros_exceed_basis` (P+C+G+fibra > 102 g, solo en `per_100g`)
  - `energy_mismatch`: |kcal − Atwater| > max(15 kcal, 20 %)
  - `sugar_exceeds_carbs`
  - `basis_uncertain`
  - `carbs_may_include_fiber`
  - `value_out_of_range`: no cabe en `Numeric(10,3)` y se guarda como desconocido.

### D12. Errores y cadena
- **Excepciones del proveedor** (se toleran: la cadena pasa al siguiente):
  - `ProviderUnavailableError`: timeout, red, 5xx, 429, cuota propia agotada, o 4xx **sin JSON** (los 400 de nginx de USDA). Trae `retry_after`.
    - Un 4xx sin JSON se loguea a nivel **error** con proveedor, status y URL (sin `api_key`). Si no fuera intermitencia de nginx sino un bug nuestro, así se vería en los logs aunque el usuario solo reciba un resultado parcial o un 503.
  - `ProviderBadDataError`: respuesta que no se puede interpretar.
- **Bug o configuración nuestra** (`ProviderError`): un 4xx **con JSON** distinto de 404/429, por ejemplo una key inválida o un parámetro mal formado. **No** se hace fallback: sube como 500.
- Ninguna excepción de `httpx` sale de `providers/food_data/_http.py`.
- `ChainedFoodDataProvider`:
  - **Barcode:** secuencial; el primero que lo tenga gana.
  - **Search:** en paralelo, concatenado en orden. Las fuentes que fallan van a `unavailable_sources`; los bugs suben.

### D13. HTTP saliente y cuotas
- **Cliente:** un solo `httpx.AsyncClient`, creado en el lifespan.
  - Timeouts: conexión 3 s, lectura 5 s, pool 2 s. Tope total de `FOOD_DATA_TIMEOUT_SECONDS` (8 s) por llamada.
  - Un solo reintento, solo para errores de **conexión** (`AsyncHTTPTransport(retries=1)`).
  - User-Agent: `Cucharon/<versión> (<FOOD_DATA_CONTACT_EMAIL>)`.
- **Cuota saliente propia** (`OutboundBudget`, en memoria):
  - OFF: 15/min de producto y 10/min de búsqueda.
  - USDA: 900/h.
  - Si el proveedor responde 429 o 503, **ese tipo de request** se pausa hasta su `Retry-After` (60 s por defecto). Una búsqueda pausada no frena los escaneos.
- **Por usuario:** barcode 30/min y search 15/min, con llave `user_id` → `429 rate_limited`.

### D14. Licencias
- Los productos de OFF (y los overrides derivados de uno) llevan `attribution {text, url, license_url}` en la respuesta.
- Los datos de OFF viven solo en `products` y nunca se mezclan con los del usuario (`user_products`) en la misma fila. Los overrides son privados por usuario y no se publican, así que el share-alike no aplica mientras no se publique la base.
- Obligaciones del cliente: ver `docs/CLIENT_NOTES.md`.

### D15. Configuración
- `FOOD_DATA_PROVIDERS` está vacío por default (opt-in explícito).
- `FOOD_DATA_PROVIDERS` vacío solo se permite con `ENV` en {`local`, `test`} (allowlist). Cualquier otro valor falla al arrancar: sin providers, el escaneo de códigos nuevos siempre daría 503.
- `off` exige `FOOD_DATA_CONTACT_EMAIL`.
- `USDA_API_KEY=DEMO_KEY` solo se permite con `ENV=local`. Sin key, USDA se desactiva con un warning.
- Sin providers, la cadena es `NoFoodDataProvider`: nunca afirma "no existe" (503 en el escaneo) y la búsqueda es solo local.

### D16. Endpoints
- `GET /products/barcode/{code}`
- `GET /products/search?q=&limit=`
- `POST /products/custom` (id opcional del cliente; override con `base_product_id`)
- `GET`, `PATCH` y `DELETE /products/custom/{id}` (borrado lógico)
- `GET /products/{id}`

## Alternativas descartadas
- **Refresco en segundo plano** (stale-while-revalidate): Render free mata las tareas y es difícil de probar.
- **Guardar resultados de búsqueda solo al elegirlos**: requeriría un endpoint extra, y las recetas necesitan ids estables.
- **Unique de barcode entre fuentes**: USDA Branded tiene códigos duplicados (D2).
- **`respx`**: `httpx.MockTransport` viene con httpx y alcanza.
- **LLM para traducir o rankear**: fuera de esta fase.
- **OFF v3.5+**: estructura de nutrientes inestable.
- **Search-a-licious**: no es oficial todavía.

## Consecuencias y riesgos
- **Cuota de OFF:** con 15 escaneos nuevos por minuto **para todos los usuarios juntos**, la caché es lo central del diseño. Si el uso crece, OFF recomienda alojar una copia local a partir de sus exports diarios.
- **IP compartida:** en Render la IP de salida es compartida con otros clientes, así que otras apps pueden gastar nuestra cuota de OFF o hacer que nos baneen.
- **`search.pl` responde 503 con frecuencia.** La búsqueda degrada a resultados locales con `partial: true`.
- **Cuotas en memoria:** valen con una sola instancia. Con más instancias, mover a Redis (`limits` lo soporta).
- **Escaneo y búsqueda hacen dos transacciones** (lectura, y escritura después del provider). Entre las dos, otro request puede escribir el mismo producto; el `ON CONFLICT` y la regla de D6 lo resuelven.
- **Glosario:** se mantiene a mano y es un punto de partida.
- **Columnas generadas:** Alembic no compara sus expresiones (avisa "Computed default … cannot be modified"). Un cambio en `search_text` requiere una migración escrita a mano.

## Pendientes
- Enviar correcciones de usuarios a OFF (lo pide su licencia/comunidad).
- Filtro por país en la búsqueda de OFF: no está documentado para `search.pl`.
- Migrar a Search-a-licious cuando sea oficial.
- Nombres en español para los resultados de USDA.
