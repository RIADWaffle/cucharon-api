# 0003. Rate limit de /auth: IP + email, y tope de concurrencia de argon2

- Estado: aceptado
- Fecha: 2026-10-14

## Contexto

- `MODULES.md` pide rate limit en `/auth/*`.
- argon2 es caro a propósito (~64 MiB y decenas de ms de CPU por hash). En Render free (512 MiB, CPU compartida), unos cuantos requests por segundo a login o registro pueden tumbar la instancia.
- Render está detrás de un proxy, así que uvicorn corre con `--proxy-headers --forwarded-allow-ips='*'`.

### Qué IP ve la app (verificado en uvicorn 0.54, `uvicorn/middleware/proxy_headers.py`)
Con `trusted_hosts="*"`, `_TrustedHosts.always_trust` es `True` y `get_trusted_client_address` regresa `x_forwarded_for_hosts[0]`: la entrada **más a la izquierda** de `X-Forwarded-For`. Ese valor lo controla el cliente (basta mandar su propio `X-Forwarded-For`), así que **la IP es spoofeable**. Un límite solo por IP no frena a un atacante que rote ese header.

## Decisión

1. **Por IP (slowapi)** en register, login, refresh y logout: `RATE_LIMIT_AUTH_IP` (default `20/minute`). Ayuda contra clientes torpes y bots simples, no contra un atacante decidido.
2. **Por email en login** (`KeyedRateLimiter` sobre `limits`): `RATE_LIMIT_LOGIN_EMAIL` (default `5/minute;20/hour`). La llave es el email normalizado, así que no depende de la IP. Se cuenta **antes** de verificar el password, para no gastar argon2 en intentos bloqueados.
3. **Tope de concurrencia de argon2**: `hash` y `verify` corren en un thread (no bloquean el event loop) a través de un `anyio.CapacityLimiter(PASSWORD_HASH_CONCURRENCY)` (default 2). Esto acota memoria y CPU **sin importar** de dónde venga la carga: es la defensa real contra el agotamiento de la instancia.
4. Si se excede un límite: `429 {"error": {"code": "rate_limited"}}`.

El almacenamiento está en memoria del proceso, válido con una sola instancia. Con más instancias habría que pasar a Redis (`limits` lo soporta).

## Consecuencias

- Un atacante puede bloquear el login de una víctima por email durante la ventana (DoS dirigido de 1 h como máximo). Es aceptable a esta escala. Si se vuelve un problema: contar solo los intentos fallidos, o usar la llave email+IP además de email.
- El registro revela si un email existe (409 `email_taken`). Es inherente al flujo de registro y queda acotado por el límite de IP y el tope de argon2.
- Pendiente evaluar: tomar la IP de la entrada más a la **derecha** de XFF, la que agrega el proxy de Render, en lugar de confiar en `*`. Requiere confirmar cuántos saltos de proxy pone Render (no está verificado).
