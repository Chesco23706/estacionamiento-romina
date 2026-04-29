# Estacionamiento Romina

Aplicacion web profesional para operar la bitacora de un estacionamiento con autenticacion segura, control por roles, registro en tiempo real, calculo automatico de tarifas, corte de caja y trazabilidad de acciones.

## Resumen ejecutivo

Esta version esta preparada para uso empresarial con:

- Backend Flask organizado con app factory.
- Base de datos compatible con SQLite para desarrollo y MySQL para produccion.
- Password hashing con Werkzeug.
- Sesiones seguras con Flask-Login.
- Proteccion CSRF en formularios.
- Seguridad HTTP con CSP, HSTS, `X-Frame-Options` y `X-Content-Type-Options`.
- Bloqueo temporal por intentos fallidos de login.
- Auditoria de acciones en base de datos.
- Docker + Gunicorn + docker-compose para despliegue.
- Endpoint `/health` para monitoreo.
- Suite basica de pruebas automatizadas.

## Arquitectura del proyecto

```text
New project 3/
|-- server.py
|-- wsgi.py
|-- config.py
|-- requirements.txt
|-- Dockerfile
|-- docker-compose.prod.yml
|-- .env.example
|-- README.md
|-- tests/
|   `-- test_app.py
`-- app/
    |-- __init__.py
    |-- decorators.py
    |-- extensions.py
    |-- models.py
    |-- pricing.py
    |-- routes.py
    |-- security.py
    |-- services.py
    |-- validators.py
    |-- static/
    |   |-- css/styles.css
    |   `-- js/app.js
    `-- templates/
        |-- base.html
        |-- login.html
        |-- dashboard.html
        |-- record_edit.html
        |-- cut_detail.html
        `-- 403.html
```

## Modulos importantes

- `app/models.py`: define tablas, relaciones y utilidades de bootstrap.
- `app/services.py`: concentra reglas de negocio para registros, empleados y cortes.
- `app/validators.py`: validaciones y sanitizacion del input.
- `app/pricing.py`: reglas de tiempo y cobro.
- `app/security.py`: endurecimiento de proxy y headers de seguridad.
- `app/routes.py`: rutas HTTP y coordinacion entre UI y servicios.
- `config.py`: configuracion por entorno.

## Roles del sistema

### Administrador

- Ver todos los registros.
- Registrar entradas y salidas.
- Marcar pagos.
- Editar y eliminar registros.
- Crear empleados.
- Activar o desactivar empleados.
- Resetear contrasenas.
- Generar cortes diarios y semanales.
- Exportar reportes.

### Empleado

- Registrar entradas de vehiculos.
- Capturar datos del vehiculo.
- Consultar la bitacora operativa.
- No puede editar tarifas.
- No puede eliminar registros.
- No puede crear usuarios.
- No puede ver ni generar cortes administrativos.

## Modelo de datos

### `roles`

- `id` PK
- `name` unico
- `description`
- `created_at`
- `updated_at`

### `users`

- `id` PK
- `full_name`
- `username` unico
- `password_hash`
- `is_active_user`
- `last_login_at`
- `failed_login_attempts`
- `locked_until`
- `role_id` FK -> `roles.id`
- `created_at`
- `updated_at`

### `tariffs`

- `id` PK
- `vehicle_type` unico
- `billing_scheme`
- `rate_amount`
- `period_unit`
- `min_charge_units`
- `notes`
- `active`
- `created_at`
- `updated_at`

### `vehicle_records`

- `id` PK
- `ticket_number` unico
- `client_name`
- `vehicle_type`
- `plate_number`
- `entry_at`
- `exit_at`
- `duration_seconds`
- `applied_rate_label`
- `total_amount`
- `status`
- `notes`
- `entry_user_id` FK -> `users.id`
- `exit_user_id` FK -> `users.id`
- `created_at`
- `updated_at`

### `cash_cuts`

- `id` PK
- `cut_type`
- `period_start`
- `period_end`
- `generated_at`
- `generated_by_user_id` FK -> `users.id`
- `total_income`
- `total_pending`
- `vehicles_served`
- `vehicles_paid`
- `breakdown_json`
- `created_at`
- `updated_at`

### `audit_logs`

- `id` PK
- `user_id` FK -> `users.id`
- `action`
- `entity_type`
- `entity_id`
- `details_json`
- `created_at`
- `updated_at`

## Reglas de cobro implementadas

- `Automovil`: `$20` por hora o fraccion.
- `Moto`: `$10` por dia o `$40` por semana completa.
- `Bicicleta`: `$10` por dia o `$40` por semana completa.
- `Carrito callejero`: `$10` por dia o `$40` por semana completa.

### Regla minima de cobro

- Si no completa una hora, `Automovil` paga 1 hora.
- Si no completa un dia, `Moto`, `Bicicleta` y `Carrito callejero` pagan 1 dia.

### Ejemplos

- 1 hora 5 minutos de automovil -> 2 horas -> `$40`.
- 3 dias 2 horas de moto -> 4 dias -> `$40`.
- 8 dias de bicicleta -> 1 semana `$40` + 1 dia `$10` -> `$50`.

## Seguridad aplicada

- Password hashing.
- Session cookies `HttpOnly`.
- `SameSite=Lax`.
- Cookies seguras en produccion.
- CSRF activo.
- Filtro y validacion de campos.
- Bloqueo temporal tras 5 intentos fallidos.
- Headers de seguridad.
- Trazabilidad de altas, bajas, pagos, cierres y cambios de usuario.
- Proteccion por roles con decoradores.

## Instalacion local para desarrollo

### 1. Crear entorno virtual

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
```

### 2. Instalar dependencias

```powershell
py -m pip install -r requirements.txt
```

### 3. Ejecutar la app

```powershell
py server.py
```

Abrir:

- [http://127.0.0.1:5000](http://127.0.0.1:5000)

### Usuarios de desarrollo

Solo en `FLASK_ENV=development` se crean usuarios demo:

- `admin` / `AdminRomina2026!`
- `empleado1` / `EmpleadoUno2026!`
- `empleado2` / `EmpleadoDos2026!`

## Despliegue profesional con Docker y MySQL

### 1. Crear archivo `.env`

Basate en [`.env.example`](C:\Users\acost\OneDrive\Documentos\New project 3\.env.example).

Ejemplo:

```env
FLASK_ENV=production
SECRET_KEY=una-clave-larga-y-aleatoria
DATABASE_URL=mysql+pymysql://romina_user:clave-segura@db:3306/romina_parking
ADMIN_BOOTSTRAP_USERNAME=admin
ADMIN_BOOTSTRAP_NAME=Administrador General
ADMIN_BOOTSTRAP_PASSWORD=una-clave-admin-segura
```

### 2. Levantar stack

```powershell
docker compose -f docker-compose.prod.yml up -d --build
```

### 3. Verificar salud

- `GET /health`

Ejemplo local:

- [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

## Despliegue recomendado para empresa

### Capa web

- Contenedor `web` con Gunicorn.
- Nginx como reverse proxy.
- Certificado TLS con Let's Encrypt o certificado corporativo.
- Solo exponer `443` y redirigir `80 -> 443`.

### Capa de datos

- MySQL administrado o contenedor dedicado con volumen persistente.
- Backups automaticos diarios.
- Usuario de base con permisos limitados solo a la app.

### Operacion

- Logs centralizados.
- Monitoreo de CPU, RAM, disco y disponibilidad.
- Rotacion de secretos.
- Cambio de contrasenas iniciales al primer acceso.

## Pruebas

Ejecutar pruebas:

```powershell
py -m pytest
```

Las pruebas actuales cubren:

- Login de administrador.
- Flujo de alta, salida, pago y corte.
- Restriccion de rutas administrativas para empleados.
- Creacion de usuarios iniciales de desarrollo.

## Explicacion tecnica sencilla

### 1. Login

Cuando un usuario envia su usuario y contrasena:

- Flask recibe el formulario.
- Busca el usuario por nombre.
- Compara el hash guardado con la contrasena enviada.
- Si coincide, crea la sesion.
- Si falla muchas veces, bloquea temporalmente la cuenta.

### 2. Registro de vehiculos

Cuando se registra una entrada:

- El formulario envia ticket, cliente, tipo y placas.
- `validators.py` limpia y valida datos.
- `services.py` crea el registro y deja audit trail.
- La tabla del dashboard muestra el vehiculo en estado `Dentro del estacionamiento`.

### 3. Tiempo en vivo

- El backend guarda `entry_at`.
- El frontend lee esa hora y recalcula cada segundo.
- Cuando se registra salida, el backend fija `exit_at` y guarda `duration_seconds`.

### 4. Corte de caja

- El administrador elige corte diario o semanal.
- El sistema filtra registros cerrados en ese periodo.
- Suma ingresos pagados, pendientes y conteo por tipo.
- Guarda el corte para consulta, exportacion o impresion.

## Checklist antes de pasar a produccion

- Cambiar `SECRET_KEY`.
- Configurar `FLASK_ENV=production`.
- Usar MySQL real.
- Configurar `ADMIN_BOOTSTRAP_PASSWORD` segura.
- Poner HTTPS.
- Restringir acceso a la base.
- Habilitar backups.
- Cambiar usuarios demo si alguna vez existieron.
- Revisar politicas de acceso del administrador.

## Siguientes mejoras empresariales recomendadas

- Recuperacion de contrasena por correo.
- Politica de expiracion de contrasenas.
- Bitacora exportable a PDF.
- Filtros por rango de fechas.
- Paginacion de registros.
- Integracion con impresora termica para tickets.
- Firma o folio de corte de caja.
- Migraciones formales con Alembic o Flask-Migrate.
