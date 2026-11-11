# Romina Propietario para Android

El APK abre `https://estacionamiento-romina.vercel.app/owner` dentro de la aplicación. En Ajustes, el botón **Abrir avisos en el navegador** abre la configuración de Web Push en un navegador compatible. En Pagos, **Descargar PDF** abre el selector de archivos de Android para elegir dónde guardar el desglose.

## Compilar y firmar

Con JDK 17, Gradle 9.3.1 y el SDK de Android 36, ejecuta desde la raíz del repositorio:

```powershell
gradle -p android assembleRelease
```

El APK sin firma queda en `android/build/outputs/apk/release/romina-owner-release-unsigned.apk`. Firma el APK con `apksigner` y la clave de lanzamiento de Romina. En el equipo de compilación, la clave está en `%USERPROFILE%\.android\romina-owner-signing\release.p12` y su contraseña está protegida por Windows en el mismo directorio. Usa la misma clave en las actualizaciones para que Android las instale sobre la versión actual.

El sitio publicado debe servir `/.well-known/assetlinks.json` con el certificado de la versión firmada para que Android pueda abrir los enlaces del panel en Romina.
