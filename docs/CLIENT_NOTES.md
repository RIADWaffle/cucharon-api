# Notas para el repo móvil

Obligaciones del cliente que salen de decisiones de la API. El contrato de los endpoints está en `openapi.json`; aquí va lo que el contrato no expresa.

## Auth: un solo refresh a la vez (ADR 0002)

- El interceptor HTTP debe tener un **mutex / single-flight** para el refresh: el primer request que recibe `401` inicia el refresh y los demás esperan esa misma promesa. Luego todos reintentan con el access token nuevo.
- Nunca dos `POST /auth/refresh` en paralelo. Si un refresh paralelo llega después de que el reemplazo ya se usó, el servidor lo trata como robo y **revoca la sesión del dispositivo**.
- Guardar el refresh token nuevo (secure storage) **antes** de usarlo. Si la respuesta se perdió o la app murió a medio refresh, reintentar con el token anterior dentro de **30 s** devuelve el mismo token nuevo. Pasado ese tiempo, el token anterior ya no sirve.
- `401 invalid_refresh_token` en el refresh = sesión terminada: mandar a login. No reintentar.

## Sync (ADR 0004)

- Llamar `synchronize()` de WatermelonDB con **`sendCreatedAsUpdated: true`**. El pull usa una ventana de solapamiento y repite registros ya recibidos; el servidor manda creados y actualizados juntos en `updated`.

## Uploads (ADR 0005, al implementar `recipes`)

- Al pedir una URL de upload, declarar `content_type` y `content_length` reales.
- Hacer el `PUT` con **exactamente** los headers de `headers` de la respuesta (incluyen `Content-Type` y `Content-Length`). Otro valor = `403`.
