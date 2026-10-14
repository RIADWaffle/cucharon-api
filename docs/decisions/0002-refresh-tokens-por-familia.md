# 0002. Refresh tokens rotativos por familia, con detección de reuso y periodo de gracia

- Estado: aceptado
- Fecha: 2026-10-14

## Contexto

`MODULES.md` pide refresh tokens rotativos y que, si se reusa uno ya rotado, se revoque toda la familia. La tabla original solo tenía `replaced_by`, así que revocar la familia obligaba a recorrer la cadena.

En móvil la red es mala: la respuesta de un refresh se puede perder después de que el servidor ya rotó el token, o la app puede morir antes de guardar el token nuevo. Sin tolerancia, el siguiente intento con el token viejo cuenta como robo y cierra la sesión del usuario.

## Decisión

### Modelo
- `refresh_tokens` tiene además `family_id` (indexado) y `created_at`.
- Cada **login** crea una familia nueva (`family_id = uuid4()`) con un token aleatorio (`secrets.token_urlsafe(32)`, 256 bits).
- Cada **refresh** emite un token con el mismo `family_id`. Al token viejo se le pone `revoked_at = now` (el momento de la rotación) y `replaced_by = <nuevo>`.
- Se guarda **solo el sha256** del token. No se usa argon2 porque el token ya tiene alta entropía y hay que buscarlo por hash.
- Access token: JWT HS256 de 15 min, con `sub`, `type=access`, `iat` y `exp`. `get_current_user` **no consulta la DB**. `is_active` se revisa en login y refresh, así que desactivar a un usuario tarda como máximo 15 min en surtir efecto.

### Sucesor determinista
El token que se emite al rotar es `HMAC-SHA256(REFRESH_DERIVATION_KEY, "refresh-rotation:" + token_anterior)` (`security.derive_rotated_refresh_token`), en base64url.

Así, dentro del periodo de gracia se puede **reentregar el mismo reemplazo** sin guardar tokens en claro ni cifrados: quien presenta el token anterior y el servidor pueden recalcularlo, nadie más. Sin la llave no se puede predecir.

#### Separación de llaves
`REFRESH_DERIVATION_KEY` es una llave propia, **distinta de `JWT_SECRET`** (`Settings` rechaza que sean iguales):
- Cada llave tiene un solo uso. Filtrar o rotar una no compromete ni invalida lo que protege la otra.
- Rotar `JWT_SECRET` (p. ej. tras una filtración) invalida los access tokens, pero no toca la gracia de los refresh.
- Rotar `REFRESH_DERIVATION_KEY` solo deja sin gracia los refresh rotados en los últimos 30 s; los access tokens y los refresh vigentes siguen funcionando.

Se eligieron dos variables en lugar de derivar ambas con HKDF desde una llave maestra: el resultado criptográfico es equivalente y así no se escribe código de derivación propio. Además cada llave se puede rotar por separado sin tocar la otra (con una llave maestra, rotarla cambia las dos).

### Veredicto (`users/domain.py::evaluate_refresh`, puro)
| Estado del token | Veredicto | Efecto |
|---|---|---|
| rotado hace < `REFRESH_TOKEN_GRACE_SECONDS` (30 s) y su reemplazo sigue `VALID` (sin rotar, sin revocar, sin expirar) | `GRACE` | regresa el **mismo** reemplazo y un access token nuevo; no rota ni revoca |
| rotado, fuera de la ventana **o** con el reemplazo ya usado/revocado/expirado | `REUSED` | revoca toda la familia, 401 |
| `revoked_at` no nulo sin `replaced_by` (logout o familia revocada) | `REVOKED` | 401 |
| `now >= expires_at` | `EXPIRED` | 401 |
| otro caso | `VALID` | rota |

- El reuso tiene prioridad sobre la expiración: un token rotado y además expirado sigue indicando robo, y la familia puede seguir viva.
- La ventana es estricta: a los 30 s exactos ya es `REUSED`. `REFRESH_TOKEN_GRACE_SECONDS=0` desactiva la gracia.
- Si `REFRESH_DERIVATION_KEY` cambió entre la rotación y el reintento, el sucesor recalculado no coincide con el guardado: se responde 401 **sin** revocar (no es señal de robo).

Hacia el cliente, todos los rechazos responden igual: `401 invalid_refresh_token`.

### Por qué la gracia no debilita la detección de robo
Si un atacante usa el token viejo dentro de la ventana, recibe el mismo reemplazo que el cliente legítimo. En cuanto uno de los dos lo use, el reemplazo rota, y el siguiente uso del otro (del token viejo o del reemplazo) ya es `REUSED`: la familia se revoca. La detección se retrasa como máximo una rotación, no se pierde.

### Concurrencia
El refresh hace `SELECT ... FOR UPDATE` sobre el token presentado y, si está rotado, también sobre su reemplazo (siempre en ese orden: viejo → nuevo, sin riesgo de deadlock). Dos refresh simultáneos con el mismo token se serializan: el segundo cae en `GRACE` y recibe el mismo reemplazo.

Aun así, **el cliente debe serializar sus refresh** (un solo refresh en vuelo; ver `docs/CLIENT_NOTES.md`). La gracia es una red de seguridad para respuestas perdidas, no un permiso para refrescar en paralelo: si un refresh paralelo llega después de que el reemplazo ya se usó, es `REUSED`.

### Logout
`POST /auth/logout {refresh_token}` revoca la familia de ese token (ese dispositivo). Es idempotente y no requiere access token. Después de un logout, el token viejo de la familia ya no entra en gracia: su reemplazo está revocado.

### Excepción a "los services no hacen commit"
`CLAUDE.md` pide un commit por request en `get_session` y rollback si hay excepción. Pero al detectar reuso hay que **persistir la revocación y a la vez responder 401**.

En el camino `REUSED`, `users/service.py::refresh` revoca la familia, hace **`session.commit()` explícito** y luego lanza `RefreshTokenReusedError`. El rollback de `get_session` ya no tiene nada pendiente. Es el único commit en un service y queda marcado con un comentario que apunta a este ADR. Cualquier otro caso requiere su propio ADR.

Alternativas descartadas:
- Un flag `commit_on_raise` en `DomainError` que `get_session` respete: esconde la excepción en una clase base que aplica a todo el proyecto. Se usó en la primera versión y se quitó para que la excepción quede en un solo lugar visible.
- Regresar un 401 sin lanzar excepción: el router tendría que conocer el caso.
- Revocar en otra sesión o transacción: complica los tests (la sesión del test no vería los datos) y abre una carrera.

## Consecuencias
- El commit de `get_session` corre con `Depends(..., scope="function")`, antes de enviar la respuesta. Con el scope default de FastAPI correría después, y un fallo de commit quedaría oculto tras un 2xx. `tests/e2e/test_session_commit.py` lo verifica.
- El e2e `test_full_auth_flow` cubre: rotación, reintento en gracia (mismo token), reuso con el reemplazo ya usado (401) y que la revocación persiste aunque la respuesta sea 401.
- Rotar `JWT_SECRET` invalida todos los access tokens; la gracia de los refresh sigue funcionando (test `test_grace_replay_survives_jwt_secret_change`).
- Rotar `REFRESH_DERIVATION_KEY` deja sin gracia los refresh rotados en los últimos 30 s: el reintento responde 401 sin revocar la familia (test `test_grace_replay_after_derivation_key_change_is_rejected_without_revoking`).
- Despliegue: `REFRESH_DERIVATION_KEY` es obligatoria. Hay que darla de alta en Render antes de desplegar esta versión.
