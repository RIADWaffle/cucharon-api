# 0005. Uploads a R2 con PUT prefirmado y `Content-Length` firmado

- Estado: aceptado (se implementa con `R2Provider` y el módulo `recipes`)
- Fecha: 2026-10-14

## Contexto

Las imágenes se suben directo del cliente a R2 con una URL prefirmada (`MODULES.md`). Hay que limitar el tamaño: si no, cualquiera con una URL válida puede subir lo que quiera a la cuenta.

En S3 esto se resuelve con un **presigned POST con policy** (`content-length-range`).

### Verificado (2026-10-14)
La documentación de presigned URLs de R2 (`developers.cloudflare.com/r2/api/s3/presigned-urls/`) lista GET, HEAD, PUT y DELETE, y dice textual: "POST (multipart form uploads via HTML forms) is not currently supported". La página de compatibilidad S3 de R2 tampoco lista `PostObject`. **No hay presigned POST con policy en R2.**

La misma documentación dice que `Content-Type` puede ir firmado y que, si el cliente manda otro, R2 responde `403 SignatureDoesNotMatch`. **No dice nada de `Content-Length`.**

## Decisión

1. **PUT prefirmado**, de vida corta (15 min).
2. El cliente pide la URL **declarando** `content_type` y `content_length` (bytes). El service valida antes de firmar: `content_type` en una lista permitida y `content_length <= máximo` del caso de uso (p. ej. imagen de receta). Si no cumple, no hay URL.
3. El provider firma `Content-Type` **y** `Content-Length` como headers firmados (SigV4, `X-Amz-SignedHeaders`) y los regresa en `PresignedUpload.headers`. El cliente los manda tal cual.
4. **Verificación después del upload**: al guardar el registro con su `image_key`, el service hace `HEAD` del objeto y revisa tamaño y tipo. Si no cumplen, borra el objeto y rechaza. Es barato y cubre el caso de que R2 no haga cumplir `Content-Length`.

## Pendiente de verificar (al implementar `R2Provider`)
- Que R2 rechace un PUT cuyo `Content-Length` no coincide con el firmado. Va como contract test (`tests/contract`, `@pytest.mark.external`): firmar N bytes, subir N+1 y esperar 403. Si R2 no lo hace cumplir, el punto 4 es la única defensa y se documenta aquí.
- Cómo pedirle al SDK que firme `Content-Length` en la URL (no asumir el parámetro sin revisar la documentación de botocore/aioboto3).

## Consecuencias
- El `Protocol` cambia de `presign_upload(key, content_type, max_bytes)` a `presign_upload(key, content_type, content_length)`: el máximo es regla de negocio del módulo, no del provider. `LocalFsProvider` y el fake se ajustan en el mismo cambio, y la ruta de upload de desarrollo debe rechazar un cuerpo de otro tamaño.
- Requisito para el cliente: mandar exactamente los headers de `PresignedUpload.headers` (anotado en `docs/CLIENT_NOTES.md`).
