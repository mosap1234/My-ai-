FROM python:3.10-slim

# تثبيت ffmpeg وأدوات النظام الأساسية مع nodejs لحل مشكلة الـ runtime
RUN apt-get update && apt-get install -y ffmpeg curl nodejs && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# تثبيت الاعتماديات من ملف المشروع
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip && python -m pip install --no-cache-dir -r /app/requirements.txt

# إنشاء مجلدات القوالب والفيديوهات داخل الحاوية
RUN mkdir -p /app/templates /app/videos

# نسخ ملفات المشروع إلى داخل الحاوية
COPY app.py /app/app.py
COPY index.html /app/templates/index.html
COPY index.html /app/index.html

EXPOSE 8080

CMD ["python", "app.py"]
