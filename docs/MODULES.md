# Módulos

Dependencias permitidas entre módulos (→ significa "puede usar la API pública de"):

```
users      → (nadie)
products   → users
recipes    → users, products
metrics    → users, products, recipes
sync       → todos (solo para serializar/aplicar cambios vía sus services)
```

No se permiten ciclos. Si aparece uno, el diseño está mal: se discute antes de seguir.

---

## users

**Responsabilidad**: identidad y datos estables de la persona. **No** incluye recetas ni comidas; cada módulo es dueño de sus propios datos.

**Tablas**
- `users`: `id, email (unique, lowercase), password_hash, is_active, created_at, updated_at`
- `refresh_tokens`: `id, user_id, family_id, token_hash, expires_at, revoked_at, replaced_by, created_at` (rotación y detección de reuso; ver ADR 0002)
- `user_profiles`: `user_id (PK), display_name, birth_date, sex, height_cm, activity_level, timezone, unit_system, created_at, updated_at`. Se crea en el registro; `timezone` es obligatoria.

`sex` se usa solo para la fórmula de BMR. `activity_level` describe el trabajo/rutina **sin contar pasos** (ver metrics).

**Endpoints**
- `POST /auth/register`, `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`
- `GET /me`, `PATCH /me/profile`

**API pública**: `get_current_user` (dependency), `get_profile(session, user_id)`.

**Reglas**
- Si reusan un refresh token ya rotado, se revoca toda la familia de tokens. Excepción: dentro de un periodo de gracia de 30 s, si el reemplazo no se ha usado, se regresa ese mismo reemplazo (ADR 0002).
- Login con error genérico ("credenciales inválidas"), sin revelar si el email existe.
- Rate limit en `/auth/*`.

---

## products

**Responsabilidad**: catálogo de alimentos con su información nutrimental. Incluye escaneo por código de barras, búsqueda por texto y productos custom. Decisiones en ADR 0006.

**Tablas**
- `products` (caché global de OFF y USDA; no sincronizable, nunca se borra): `id, source (off|usda), source_type (dataType de USDA), external_id, barcode (GTIN-14, nullable, índice no único), name, brand, source_url, basis (per_100g|per_100ml), kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg, extra_nutrients (jsonb), serving_quantity, serving_unit (g|ml), quality_flags (text[]), search_aliases, search_text (generada), fetched_via (barcode|search), fetched_at, created_at, updated_at`
  - Unique `(source, external_id)`. Solo OFF guarda `barcode`, y su `external_id` es ese mismo GTIN-14 (CHECK `barcode_only_off`); juntos garantizan que `barcode` es único sin un segundo índice único, que rompería el `ON CONFLICT` (ADR 0006, D5).
- `user_products` (sincronizable): `id (acepta id del cliente), user_id, base_product_id (nullable), name, brand, barcode (GTIN-14), basis, <nutrientes>, extra_nutrients, serving_quantity, serving_unit, density_g_per_ml, unit_weight_g, search_text (generada), created_at, updated_at, deleted_at`
  - Únicos parciales (solo filas activas): `(user_id, barcode)` y `(user_id, base_product_id)`.
- `product_barcode_misses`: `barcode (PK), checked_at`. Caché negativa.

Si `base_product_id` está presente, es un **override** del producto global (p. ej. porque OFF tiene datos incorrectos): al crearlo se copia el producto base y se aplican los cambios. Las búsquedas y escaneos del usuario muestran su override en lugar del producto base.

Nutrientes principales como columnas: `kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg`, **por 100 g o por 100 ml según `basis`**. `carbs_g` son carbohidratos **disponibles** (sin fibra), como OFF y la NOM-051. El resto va en `extra_nutrients`.

**Endpoints** (todos requieren auth)
- `GET /products/barcode/{code}`: UPC-A, EAN-8, EAN-13 o GTIN-14 con dígito verificador válido (si no, 422 sin llamar al provider). Orden: override/custom del usuario → caché fresca → caché negativa → provider. Si lo encuentra en el provider, lo guarda (`ON CONFLICT`). Si no existe en ningún lado, responde 404 para que el cliente ofrezca crear uno custom. Si el provider falla, regresa la copia vieja (`stale: true`) o 503 con `Retry-After`.
- `GET /products/search?q=&limit=`: productos del usuario y caché local primero (sin acentos, trigramas); OFF y USDA solo si lo local no alcanza. Responde `{items, partial}`.
- `POST /products/custom`, `GET|PATCH|DELETE /products/custom/{id}` (borrado lógico)
- `GET /products/{id}`: producto de la caché global (lo usan recetas).

**API pública**: `get_product(session, id)`, `normalize_gtin`, `NutrientBasis`, `ServingUnit`, `ProductView`.

**Usa**: `FoodDataProvider`.

**Reglas**
- Los datos de proveedores vienen incompletos con frecuencia. `nutrition_complete` exige kcal, proteína, carbohidratos y grasa; lo que falta queda `null`, no se inventa.
- Los valores se normalizan a 100 g / 100 ml dentro del provider, no en el módulo.
- **Nunca se asume una densidad.** Convertir g ↔ ml solo es posible con `density_g_per_ml` del usuario. Impacta recipes y metrics (ADR 0006, D9): sus cantidades deben ir en la unidad de la base del producto.
- Los datos sospechosos se marcan en `quality_flags`; no se corrigen.
- Una búsqueda nunca degrada la caché: no sobrescribe datos de un escaneo ni datos más completos (`domain.search_upsert_action`).
- Rate limit por usuario en barcode y search (pueden pegarle a APIs externas con cuota compartida).

---

## recipes

**Responsabilidad**: recetas con ingredientes, pasos e imágenes. También calcula la nutrición total y por porción, y escala porciones.

**Tablas** (todas sincronizables)
- `recipes`: `id, user_id, name, description, base_servings, prep_minutes, cook_minutes, cover_image_key, is_quick_log, ...`
- `recipe_ingredients`: `id, recipe_id, position, product_ref_type (product|user_product|none), product_ref_id, display_name, quantity (Numeric, nullable = "al gusto"), unit, grams (Numeric, nullable), note`
- `recipe_steps`: `id, recipe_id, position, text, image_key, timer_seconds`

`is_quick_log` sirve para comidas frecuentes (por ejemplo, un licuado diario) que se registran con un solo tap.

**domain.py**
- `to_grams(quantity, unit, product) -> Decimal | None`: g/kg directo; ml/l usando `density_g_per_ml`; `pieza` usando `unit_weight_g`; tazas/cucharadas convierten a ml y luego a gramos. Si no se puede convertir, regresa `None`.
- `compute_nutrition(ingredients) -> RecipeNutrition`: totales, por porción y `incomplete_ingredients` (lista de ingredientes sin gramos o sin producto).
- `scale(recipe, target_servings)`: factor = `target / base`. Los ingredientes "al gusto" quedan igual. Las unidades `pieza` se redondean a 0.5 y se marcan `rounded=true`.

**Endpoints**
- CRUD de recetas (ingredientes y pasos anidados)
- `GET /recipes/{id}?servings=N`: receta escalada con nutrición
- `POST /recipes/{id}/images/upload-url`: URL prefirmada (`StorageProvider`)

**Reglas**
- La nutrición **siempre se calcula**, nunca la escribe el usuario ni la IA.
- Las mismas fórmulas existen en móvil (para offline). Se validan con `docs/test-vectors/scaling.json` y `nutrition.json`.

---

## metrics

**Responsabilidad**: datos corporales, metas, diario de comidas, actividad, presupuesto calórico diario y el agente.

**Tablas** (sincronizables)
- `body_measurements`: `id, user_id, measured_at, weight_kg, body_fat_pct (nullable)`
- `goals`: `id, user_id, goal_type (lose|maintain|gain), target_rate_kg_per_week, starts_on, ends_on (nullable)`
- `food_log_entries`: `id, user_id, eaten_at, meal_type, source_type (recipe|product|user_product|free_text), source_id, servings, grams, raw_text, status (confirmed|pending_analysis|needs_review), nutrition_snapshot (columnas kcal/macros)`
- `activity_daily`: `id, user_id, date, steps, active_kcal, source (health_connect|manual)`. Hay un solo registro por (user, date, source) y se hace upsert.

**domain.py**
- `bmr_mifflin_st_jeor(weight, height, age, sex)`
- `tdee(bmr, activity_level)`: el multiplicador **no contempla pasos**.
- `daily_budget(tdee, goal, active_kcal, activity_credit_factor)`: suma solo una fracción (default 0.5, configurable) de las kcal activas del dispositivo. Esto evita contar la actividad dos veces y compensa que las pulseras sobreestiman.
- Guardrails: el objetivo nunca baja de `max(bmr, piso_por_sexo)` y el ritmo de pérdida está limitado a ~1% del peso corporal por semana. Si la meta pedida los rompe, se ajusta y se explica por qué.
- `day_summary(entries, activity, budget, tz)` y `weight_trend(measurements)` (promedio móvil de 7 días).

**Registro por texto con IA** (`POST /food-log/parse`)
1. `LLMProvider.generate_structured(text → [{name, quantity, unit}])`
2. Cada item se resuelve contra quick-logs/recetas del usuario → sus productos → `FoodDataProvider.search`.
3. Se responde con una propuesta, **no se guarda**. El cliente confirma y luego llama a `POST /food-log`.

Si se registra sin internet, la entrada queda en `pending_analysis` y se analiza cuando se sincroniza.

**Agente** (`POST /metrics/agent/chat`)
- Usa tool-calling con tools **de solo lectura**: `get_profile`, `get_day_summary(date)`, `get_weight_trend(days)`, `get_goal`, `compute_budget(goal_params)`.
- `propose_goal` regresa una propuesta. Para aplicarla, se usa un endpoint normal que requiere confirmación del usuario.
- El LLM solo explica y recomienda usando los números que regresan las tools. El prompt de sistema le prohíbe calcular por su cuenta y le indica sugerir un profesional de salud ante condiciones médicas.
- Rate limit por usuario.

**Datos de Health Connect**: la app móvil los lee y los manda a `PUT /activity/{date}`. El backend **no** habla con Xiaomi ni con Google.

---

## sync

**Responsabilidad**: protocolo de sincronización de WatermelonDB.

- `GET /sync/pull?last_pulled_at=&schema_version=`: regresa `{changes: {table: {created, updated, deleted}}, timestamp}` con todo lo que tenga `updated_at > last_pulled_at` del usuario.
- `POST /sync/push`: aplica cambios en una sola transacción. Usa last-write-wins por registro, es idempotente (reaplicar el mismo push no duplica nada) y valida ownership (`user_id`) de cada registro y de sus referencias.
- Las tablas sincronizables están en una lista explícita, y cada módulo expone `apply_sync_changes` y `pull_changes` en su API pública. Sync no toca tablas de otros módulos directamente.
- Las imágenes **no** viajan por sync. Se suben por URL prefirmada y el registro guarda `image_key`.
- `timestamp` del servidor, nunca del cliente.
- El pull usa una ventana de solapamiento (`updated_at > last_pulled_at - overlap`) porque `updated_at` se asigna antes del commit; `clock_timestamp()` no lo resuelve. El cliente usa `sendCreatedAsUpdated: true`. Ver ADR 0004.

---

## providers

Cada provider tiene `base.py` (Protocol + tipos propios), implementaciones y `factory.py` (elige por settings). Los tipos de los SDKs externos **nunca** salen del provider.

### llm: `LLMProvider`
- `generate_structured(messages, schema: type[T]) -> T`
- `chat(messages, tools) -> LLMResponse` (con tipos propios de `ToolCall` y `Message`)
- Implementación: `OpenAICompatibleProvider(base_url, api_key, model)`. Cubre Groq, OpenRouter, Ollama, etc. Cambiar de proveedor es cuestión de configuración.
- Config: `LLM_PROVIDER, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL`

### food_data: `FoodDataProvider`
- `get_by_barcode(gtin14) -> ProductData | None`, `search(query, limit) -> SearchResult(items, unavailable_sources)`
- Implementaciones: `OpenFoodFactsProvider` (barcode y búsqueda; API v3.4 fijada) y `UsdaProvider` (alimentos genéricos; no resuelve códigos; glosario ES→EN para consultas en español).
- `ChainedFoodDataProvider([...])` prueba en orden (barcode) o en paralelo (search). Pasa al siguiente con `None`, `ProviderUnavailableError` o `ProviderBadDataError`; un `ProviderError` (bug o config) sube.
- Un solo `httpx.AsyncClient` (lifespan) con timeouts, User-Agent con contacto y cuota saliente por proveedor (`OutboundBudget`).
- Config: `FOOD_DATA_PROVIDERS, FOOD_DATA_CONTACT_EMAIL, OFF_*, USDA_*` (ver `.env.example` y ADR 0006).

### storage: `StorageProvider`
- `presign_upload(key, content_type, max_bytes)`, `presign_download(key)`, `delete(key)`
- Implementaciones: `R2Provider` y `LocalFsProvider` (para desarrollo).
- R2 no soporta presigned POST con policy: el upload es PUT prefirmado con `Content-Type` y `Content-Length` firmados, más `HEAD` de verificación al guardar. La firma pasará a `presign_upload(key, content_type, content_length)`. Ver ADR 0005.

Cada provider tiene un fake en `tests/fakes/` y un contract test en `tests/contract/`.
