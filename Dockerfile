FROM python:3.10-slim

# تثبيت ffmpeg وأدوات النظام الأساسية مع nodejs لحل مشكلة الـ runtime
RUN apt-get update && apt-get install -y ffmpeg curl nodejs && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# تثبيت الفلاسك وأداة تحميل اليوتيوب yt-dlp
RUN pip install flask yt-dlp

# إنشاء مجلدات القوالب والفيديوهات داخل الحاوية
RUN mkdir -p /app/templates /app/videos

# نسخ ملفات المشروع من جيت هاب إلى داخل الحاوية
COPY app.py /app/app.py
COPY index.html /app/templates/index.html
# نسخ ملف الكوكيز إن وجد
COPY cookies.txt* /app/

EXPOSE 8080

CMD ["python", "app.py"]
