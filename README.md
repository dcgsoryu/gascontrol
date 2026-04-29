# 🔥 GasControl

Aplicación web para el control de consumo de gas doméstico con tarifa TUR2.

## Características

- 📊 Gráficas de consumo (m³, kWh) y gasto (€) con filtro de fechas
- 📝 Introducción manual de lecturas del contador
- 📥 Importación de lecturas históricas desde CSV
- 📤 Exportación a CSV, PDF e informe SQL
- 💶 Precio automático desde API precios-tur.petru.tech
- ⚙️ Factor de conversión y cuota fija configurables
- 📋 Historial paginado (10 / 50 / 100 por página)
- 🐳 Desplegable con Docker Compose en Raspberry Pi 3B

---

## Estructura del proyecto

```
gascontrol/
├── app/
│   └── main.py          # Backend FastAPI
├── templates/
│   └── index.html       # Frontend completo
├── static/              # Archivos estáticos (CSS/JS adicional si se añade)
├── data/                # Base de datos SQLite (generada automáticamente)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## 🚀 Despliegue en Raspberry Pi 3B

### Requisitos previos

```bash
# En tu Raspberry Pi (Raspberry Pi OS 64-bit recomendado)
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose git
sudo usermod -aG docker $USER
# Cerrar sesión y volver a entrar para que el grupo surta efecto
```

### Clonar y desplegar

```bash
# Clona el repositorio (o copia los archivos)
git clone <tu-repositorio> gascontrol
cd gascontrol

# Construir y arrancar
docker compose up -d --build

# Ver logs
docker compose logs -f
```

La aplicación estará disponible en: **http://<ip-raspberry>:8000**

### Comandos útiles

```bash
# Parar la aplicación
docker compose down

# Actualizar tras cambios de código
docker compose up -d --build

# Ver logs en tiempo real
docker compose logs -f gascontrol

# Backup de la base de datos
docker cp gascontrol:/app/data/gascontrol.db ./backup_$(date +%Y%m%d).db

# Restaurar backup
docker cp ./backup_YYYYMMDD.db gascontrol:/app/data/gascontrol.db
docker compose restart
```

---

## 💻 Desarrollo en GitHub Codespaces

```bash
# Instalar dependencias
pip install -r requirements.txt

# Arrancar en modo desarrollo
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Accede en el panel de puertos de Codespaces al puerto **8000**.

---

## 📄 Formato CSV para importar

```csv
fecha,valor_contador,nota
20/12/2023 12:00,269.00,Lectura histórica
15/01/2024,285.50,Lectura mensual
```

Formatos de fecha aceptados:
- `DD/MM/YYYY HH:MM`
- `DD/MM/YYYY`
- `YYYY-MM-DD`
- `YYYY-MM-DD HH:MM:SS`

Descarga la plantilla desde la propia aplicación con el botón **📋 Plantilla CSV**.

---

## ⚙️ Configuración por defecto

| Parámetro | Valor | Descripción |
|---|---|---|
| Factor conversión | 10.23 | m³ → kWh (ajusta según tu última factura) |
| Cuota fija | 10.50 € | Cuota fija mensual estimada |
| Precio kWh | 0.0650 €/kWh | Precio manual (se actualiza con botón TUR) |
| Tarifa | TUR2 | Tarifa de Último Recurso |

Todos estos valores son configurables desde la interfaz web.

---

## 🔌 API endpoints

| Método | Endpoint | Descripción |
|---|---|---|
| GET | `/` | Interfaz web |
| GET | `/api/resumen` | KPIs generales |
| GET | `/api/lecturas` | Historial paginado |
| POST | `/api/lecturas` | Nueva lectura |
| DELETE | `/api/lecturas/{id}` | Eliminar lectura |
| GET | `/api/grafica` | Datos para gráficas |
| GET | `/api/config` | Configuración actual |
| POST | `/api/config` | Actualizar configuración |
| GET | `/api/precio-tur` | Precio TUR desde API externa |
| GET | `/api/export/csv` | Exportar lecturas CSV |
| GET | `/api/export/pdf` | Exportar informe PDF |
| GET | `/api/export/sql` | Backup SQL |
| GET | `/api/export/template` | Plantilla CSV |
| POST | `/api/import/csv` | Importar lecturas CSV |

---

## 📝 Notas

- Los datos se persisten en un volumen Docker (`gascontrol_data`), sobreviven a reinicios y rebuilds.
- La Raspberry Pi 3B tiene 1GB RAM; el contenedor está limitado a 256MB para no saturar el sistema.
- Se recomienda Raspberry Pi OS 64-bit para mejor compatibilidad con la imagen Python slim.
- El factor de conversión varía según la zona geográfica y período; actualízalo con el valor de tu última factura.
