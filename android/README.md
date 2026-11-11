# Romina Propietario para Android

El APK usa una Trusted Web Activity para abrir `https://estacionamiento-romina.vercel.app/owner`. La aplicación comprueba el manifiesto de Romina antes de abrir el panel. Una vez publicado el backend, conserva el acceso al mismo servicio y a los avisos Web Push configurados en el panel.

## Compilar y firmar

Con JDK 17, Gradle 9.3.1 y el SDK de Android 36, ejecuta desde la raíz del repositorio:

```powershell
gradle -p android assembleRelease
```

El APK sin firma queda en `android/build/outputs/apk/release/romina-owner-release-unsigned.apk`. Firma el APK con `apksigner` y la clave de lanzamiento de Romina. En el equipo de compilación, la clave está en `%USERPROFILE%\.android\romina-owner-signing\release.p12` y su contraseña está protegida por Windows en el mismo directorio. Usa la misma clave en las actualizaciones para que Android las instale sobre la versión actual.

El sitio publicado debe servir `/.well-known/assetlinks.json` con el certificado de la versión firmada. Chrome usa esa asociación para abrir el panel en modo de aplicación y habilitar la delegación de notificaciones.
