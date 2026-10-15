# Notas para el repo móvil

Obligaciones del cliente que salen de decisiones de la API. El contrato de los endpoints está en `openapi.json`; aquí va lo que el contrato no expresa.

## Auth: un solo refresh a la vez (ADR 0002)

- El interceptor HTTP debe tener un **mutex / single-flight** para el refresh: el primer request que recibe `401` inicia el refresh y los demás esperan esa misma promesa. Luego todos reintentan con el access token nuevo.
- Nunca dos `POST /auth/refresh` en paralelo. Si un refresh paralelo llega después de que el reemplazo ya se usó, el servidor lo trata como robo y **revoca la sesión del dispositivo**.
- Guardar el refresh token nuevo (secure storage) **antes** de usarlo. Si la respuesta se perdió o la app murió a medio refresh, reintentar con el token anterior dentro de **30 s** devuelve el mismo token nuevo. Pasado ese tiempo, el token anterior ya no sirve.
- `401 invalid_refresh_token` en el refresh = sesión terminada: mandar a login. No reintentar.

## Sync (ADR 0004)

- Llamar `synchronize()` de WatermelonDB con **`sendCreatedAsUpdated: true`**. El pull usa una ventana de solapamiento y repite registros ya recibidos; el servidor manda creados y actualizados juntos en `updated`.

## Products (ADR 0006)

- **Atribución de Open Food Facts (obligatoria, licencia ODbL).** Cuando un producto trae `attribution` (productos de OFF y overrides hechos a partir de uno):
  - Mostrar `attribution.text` con un enlace a `attribution.url` (la página del producto en OFF) en la pantalla del producto y en los resultados de búsqueda.
  - En "Acerca de" / "Ajustes": "Contiene datos de Open Food Facts (openfoodfacts.org), disponibles bajo la Open Database License", con enlaces a https://openfoodfacts.org y a `attribution.license_url`.
  - USDA es dominio público: basta una mención de cortesía en "Acerca de" ("U.S. Department of Agriculture, FoodData Central").
  - No exportar ni publicar la base de productos de OFF sin revisar la licencia (share-alike).
- **Buscar al enviar, no mientras se escribe.** OFF permite 10 búsquedas por minuto para todo el backend. Mínimo 2 caracteres.
- **Escaneo:**
  - Mandar el código tal como lo da el lector (UPC-A, EAN-8, EAN-13 o GTIN-14). **UPC-E se expande a UPC-A antes de mandarlo**; si no, responde 422 `invalid_barcode`.
  - `barcode` regresa como GTIN-14 (14 dígitos).
  - `404 product_not_found`: ofrecer crear un producto custom con ese código (`POST /products/custom` con `barcode`).
  - `503 food_data_unavailable`: reintentar después de `Retry-After`; ofrecer crear un custom.
  - `stale: true`: datos de una copia vieja (la fuente no respondió); mostrar un aviso discreto.
- **Búsqueda:** `partial: true` = alguna fuente externa no respondió; los resultados pueden estar incompletos.
- **Base de los nutrientes:** todos los valores son por 100 g o por 100 ml según `basis`. **Nunca asumir 1 g/ml.** Para convertir entre g y ml solo se usa `density_g_per_ml` (productos del usuario). Mostrar "por 100 ml" en bebidas.
- **Calidad:** si `quality_flags` no está vacío, mostrar un aviso ("los datos de este producto podrían ser incorrectos") y sugerir crear una versión propia (override con `base_product_id`). Si `nutrition_complete` es `false`, faltan kcal o macros: no usar el producto para calcular totales sin avisar.
- **Productos propios:** se pueden crear con un `id` generado en el cliente (sync offline). Al borrar se marca `deleted_at` (borrado lógico).

## Uploads (ADR 0005, al implementar `recipes`)

- Al pedir una URL de upload, declarar `content_type` y `content_length` reales.
- Hacer el `PUT` con **exactamente** los headers de `headers` de la respuesta (incluyen `Content-Type` y `Content-Length`). Otro valor = `403`.
