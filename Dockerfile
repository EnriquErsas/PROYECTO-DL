# Usar imagen base ligera de Python
FROM python:3.10-slim

# Evitar que Python genere archivos .pyc y buffer de salida
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Instalar dependencias del sistema necesarias (FFmpeg es CRÍTICO)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Crear un usuario no-root para seguridad (Requisito de HF Spaces)
RUN useradd -m -u 1000 user

# Establecer directorio de trabajo
WORKDIR /app

# Copiar requirements y la aplicación
COPY --chown=user requirements.txt .
COPY --chown=user . .

# Cambiar al usuario no-root
USER user

# Instalar dependencias de Python
ENV PATH="/home/user/.local/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Exponer el puerto (Railway usa variable de entorno PORT)
EXPOSE 8080

# Comando para ejecutar la aplicación usando el puerto dinámico
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
