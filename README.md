# Video Downloader Local (FastAPI + yt-dlp)

Esta aplicación permite descargar videos de YouTube y otros sitios soportados por yt-dlp en tu máquina local.

## Prerrequisitos

1. **Python 3.8+** instalado.
2. **FFmpeg** instalado y accesible en tu variable de entorno PATH (necesario para unir video y audio).
   - En Windows: `winget install ffmpeg` o descarga de [ffmpeg.org](https://ffmpeg.org/download.html) y añade `bin/` al PATH.

## Instalación

1.  Abre una terminal en esta carpeta.
2.  Crea un entorno virtual (opcional pero recomendado):
    ```bash
    python -m venv venv
    .\venv\Scripts\activate
    ```
3.  Instala las dependencias:
    ```bash
    pip install -r requirements.txt
    ```

## Ejecución

Para iniciar el servidor, ejecuta:

```bash
uvicorn main:app --reload
```

O simplemente corre el script de Python directamente:

```bash
python main.py
```

La aplicación estará disponible en: **http://127.0.0.1:8000**

## Uso

1.  Ingresa a http://127.0.0.1:8000 en tu navegador.
2.  Pega la URL del video (YouTube, Vimeo, HLS stream, etc.).
3.  Haz clic en "Procesar Descarga".
4.  Espera mientras el backend descarga y procesa el video.
5.  El archivo se descargará automáticamente a tu carpeta de Descargas del navegador.
6.  El archivo temporal en el servidor se eliminará automáticamente.
