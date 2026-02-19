# Usar imagen base ligera de Python
FROM python:3.10-slim

# Evitar que Python genere archivos .pyc y buffer de salida
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Instalar dependencias del sistema (FFmpeg + Node.js para yt-dlp EJS)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Verificar que node está en /usr/bin (accesible para TODOS los usuarios)
RUN which node && node --version && echo "Node OK: $(node --version)"

# Hacer symlink explícito por si acaso
RUN ln -sf $(which node) /usr/local/bin/node && ln -sf $(which npm) /usr/local/bin/npm

# Crear un usuario no-root para seguridad
RUN useradd -m -u 1000 user

# Establecer directorio de trabajo
WORKDIR /app

# Copiar requirements y la aplicación
COPY --chown=user requirements.txt .
COPY --chown=user . .

# Cambiar al usuario no-root
USER user

# PATH que incluye tanto local como sistema
ENV PATH="/home/user/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Instalar dependencias de Python (incluye yt-dlp-ejs)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Verificar que yt-dlp ve Node.js desde el usuario no-root
RUN node --version && python -c "import yt_dlp; print('yt-dlp OK:', yt_dlp.version.__version__)"

# Exponer el puerto (Railway usa variable de entorno PORT)
EXPOSE 8080

# Comando para ejecutar la aplicación usando el puerto dinámico
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
