from flask import Flask, render_template, render_template_string, request, jsonify, session, Response, stream_with_context, redirect, send_from_directory
import subprocess
import os
import signal
import threading
import time
import random
import shutil
import json

from apscheduler.schedulers.background import BackgroundScheduler
from jinja2 import TemplateNotFound

app = Flask(__name__, template_folder='.')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'super-secret-key-12345')

VIDEO_DIR = "/app/videos"
os.makedirs(VIDEO_DIR, exist_ok=True)
ffmpeg_process = None

# Playlist / Queue state
playlist = []  # list of filenames
playlist_lock = threading.Lock()
playlist_thread = None
playlist_running = False
playlist_control = {
    'shuffle': False,
    'repeat': False
}

# Scheduler
scheduler = BackgroundScheduler()
scheduler.start()


# جلب كلمة المرور من متغيرات البيئة في Railway
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')


def render_index_template(**context):
    """Render the UI template, with a file-based fallback if Flask can't locate templates."""
    try:
        return render_template('index.html', **context)
    except TemplateNotFound:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        candidate_paths = [
            os.path.join(base_dir, 'templates', 'index.html'),
            os.path.join(base_dir, 'index.html'),
            '/app/templates/index.html',
            '/app/index.html',
        ]
        for template_path in candidate_paths:
            if os.path.exists(template_path):
                with open(template_path, 'r', encoding='utf-8') as template_file:
                    return render_template_string(template_file.read(), **context)
        return "Template not found", 500

def get_video_meta(filename):
    """جلب حجم ومدّة الفيديو باستخدام ffprobe"""
    path = os.path.join(VIDEO_DIR, filename)
    if not os.path.exists(path):
        return {"size": "0 MB", "duration": "00:00"}
    
    size_mb = round(os.path.getsize(path) / (1024 * 1024), 1)
    
    try:
        cmd = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{path}"'
        duration_sec = float(subprocess.check_output(cmd, shell=True).decode().strip())
        mins, secs = divmod(int(duration_sec), 60)
        hrs, mins = divmod(mins, 60)
        duration_str = f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs else f"{mins:02d}:{secs:02d}"
    except:
        duration_str = "--:--"
        
    return {"size": f"{size_mb} MB", "duration": duration_str}


def start_ffmpeg(video_path, stream_key, loop=False):
    """Start ffmpeg for a single file and return the Popen object."""
    rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
    loop_flag = "-stream_loop -1 " if loop else ""
    cmd = f'ffmpeg -re {loop_flag}-i "{video_path}" -c:v copy -c:a copy -f flv "{rtmp_url}"'
    proc = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid)
    return proc


def playlist_runner(stream_key):
    global ffmpeg_process, playlist_running
    while True:
        with playlist_lock:
            if not playlist or not playlist_running:
                break
            # determine next index
            if playlist_control['shuffle']:
                idx = random.randrange(len(playlist))
            else:
                idx = 0
            video_file = playlist.pop(idx) if not playlist_control['repeat'] else playlist[idx]

        video_path = os.path.join(VIDEO_DIR, video_file)
        if not os.path.exists(video_path):
            # skip missing files
            continue

        try:
            ffmpeg_process = start_ffmpeg(video_path, stream_key, loop=False)
            # wait for process to finish unless stopped
            rc = ffmpeg_process.wait()
        except Exception:
            pass
        finally:
            ffmpeg_process = None

        # if repeat is enabled and not shuffle, keep the same list
        if playlist_control['repeat'] and not playlist_control['shuffle']:
            time.sleep(0.1)
            continue

    playlist_running = False


@app.route('/')
def index():
    if ADMIN_PASSWORD and not session.get('logged_in'):
        return render_index_template(login_required=True)
        
    raw_videos = [f for f in os.listdir(VIDEO_DIR) if f.endswith(('.mp4', '.mkv', '.avi', '.mov'))]
    videos_with_meta = []
    for v in raw_videos:
        meta = get_video_meta(v)
        videos_with_meta.append({
            "name": v,
            "size": meta["size"],
            "duration": meta["duration"]
        })
        
    return render_index_template(videos=videos_with_meta, streaming=ffmpeg_process is not None, login_required=False)

@app.route('/login', methods=['POST'])
def login():
    password = request.form.get('password')
    if password == ADMIN_PASSWORD:
        session['logged_in'] = True
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": "كلمة المرور غير صحيحة!"})

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect('/')

@app.route('/upload_cookies', methods=['POST'])
def upload_cookies():
    if ADMIN_PASSWORD and not session.get('logged_in'):
        return jsonify({"status": "error", "message": "غير مصرح لك"})
        
    if 'cookies_file' not in request.files:
        return jsonify({"status": "error", "message": "لم يتم اختيار أي ملف!"})
        
    file = request.files['cookies_file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "اسم الملف فارغ!"})
        
    try:
        file.save("/app/cookies.txt")
        return jsonify({"status": "success", "message": "🍪 تم رفع وتحديث ملف الكوكيز بنجاح في السيرفر!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/start', methods=['POST'])
def start_stream():
    global ffmpeg_process
    if ffmpeg_process is not None:
        return jsonify({"status": "error", "message": "البث يعمل بالفعل حالياً!"})

    stream_key = request.form.get('stream_key')
    video_file = request.form.get('video_file')
    loop_enabled = request.form.get('loop') == 'true'

    if not stream_key or not video_file:
        return jsonify({"status": "error", "message": "الرجاء إدخال مفتاح البث واختيار ملف الفيديو."})

    video_path = os.path.join(VIDEO_DIR, video_file)
    rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"

    loop_flag = "-stream_loop -1 " if loop_enabled else ""
    cmd = f'ffmpeg -re {loop_flag}-i "{video_path}" -c:v copy -c:a copy -f flv "{rtmp_url}"'

    try:
        ffmpeg_process = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid)
        return jsonify({"status": "success", "message": "🚀 تم بدء البث بنجاح!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/stop', methods=['POST'])
def stop_stream():
    global ffmpeg_process
    if ffmpeg_process is None:
        return jsonify({"status": "error", "message": "لا يوجد بث نشط لإيقافه."})

    try:
        os.killpg(os.getpgid(ffmpeg_process.pid), signal.SIGTERM)
        ffmpeg_process = None
        return jsonify({"status": "success", "message": "🛑 تم إيقاف البث بنجاح."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/delete', methods=['POST'])
def delete_video():
    if ADMIN_PASSWORD and not session.get('logged_in'):
        return jsonify({"status": "error", "message": "غير مصرح لك"})
        
    video_file = request.form.get('video_file')
    if not video_file:
        return jsonify({"status": "error", "message": "اسم الملف غير صحيح."})
        
    try:
        path = os.path.join(VIDEO_DIR, video_file)
        if os.path.exists(path):
            os.remove(path)
            return jsonify({"status": "success", "message": "🗑️ تم حذف الملف بنجاح!"})
        return jsonify({"status": "error", "message": "الملف غير موجود أصلاً."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route('/download')
def download_file():
    if ADMIN_PASSWORD and not session.get('logged_in'):
        return jsonify({"status": "error", "message": "غير مصرح لك"}), 403
    filename = request.args.get('file')
    if not filename:
        return jsonify({"status": "error", "message": "اسم الملف مفقود."}), 400
    if '..' in filename or filename.startswith('/'):
        return jsonify({"status": "error", "message": "اسم ملف غير صالح."}), 400
    path = os.path.join(VIDEO_DIR, filename)
    if not os.path.exists(path):
        return jsonify({"status": "error", "message": "الملف غير موجود."}), 404
    return send_from_directory(VIDEO_DIR, filename, as_attachment=True)

@app.route('/download_progress')
def download_progress():
    youtube_url = request.args.get('url')
    if not youtube_url:
        return Response("data: خطأ: الرابط فارغ\n\n", mimetype='text/event-stream')

    cookies_path = "/app/cookies.txt"
    cookies_flag = f'--cookies "{cookies_path}"' if os.path.exists(cookies_path) else ''

    # تم إضافة الإعدادات الاحترافية لتسريع التحميل بشكل خارق عبر 10 خيوط تحميل متوازية وتعديل الفرمتة
    cmd = (
        f'yt-dlp {cookies_flag} --js-runtimes node --remote-components ejs:github --newline '
        f'--concurrent-fragments 10 --buffer-size 16K '
        f'-P "{VIDEO_DIR}" -f "bv*[ext=mp4]+ba[ext=m4a]/best[ext=mp4]/best" "{youtube_url}"'
    )

    def generate():
        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in iter(process.stdout.readline, ''):
            if line:
                yield f"data: {line.strip()}\n\n"
        process.stdout.close()
        return_code = process.wait()
        if return_code == 0:
            yield "data: [DONE] تم التحميل بنجاح واكتملت العملية!\n\n"
        else:
            yield "data: [ERROR] فشل التحميل، تأكد من حماية الرابط أو الكوكيز.\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.route('/playlist/add', methods=['POST'])
def playlist_add():
    if ADMIN_PASSWORD and not session.get('logged_in'):
        return jsonify({"status": "error", "message": "غير مصرح لك"})
    video_file = request.form.get('video_file')
    if not video_file:
        return jsonify({"status": "error", "message": "اسم الملف غير صحيح."})
    path = os.path.join(VIDEO_DIR, video_file)
    if not os.path.exists(path):
        return jsonify({"status": "error", "message": "الملف غير موجود."})
    with playlist_lock:
        playlist.append(video_file)
    return jsonify({"status": "success", "message": "تمت إضافة الملف لقائمة التشغيل.", "playlist": playlist})


@app.route('/playlist/remove', methods=['POST'])
def playlist_remove():
    if ADMIN_PASSWORD and not session.get('logged_in'):
        return jsonify({"status": "error", "message": "غير مصرح لك"})
    video_file = request.form.get('video_file')
    if not video_file:
        return jsonify({"status": "error", "message": "اسم الملف غير صحيح."})
    with playlist_lock:
        try:
            playlist.remove(video_file)
        except ValueError:
            return jsonify({"status": "error", "message": "الملف غير موجود في القائمة."})
    return jsonify({"status": "success", "message": "تمت إزالته من القائمة.", "playlist": playlist})


@app.route('/playlist/list')
def playlist_list():
    return jsonify({"playlist": playlist, "shuffle": playlist_control['shuffle'], "repeat": playlist_control['repeat']})


@app.route('/playlist/start', methods=['POST'])
def playlist_start():
    global playlist_thread, playlist_running
    if ADMIN_PASSWORD and not session.get('logged_in'):
        return jsonify({"status": "error", "message": "غير مصرح لك"})
    stream_key = request.form.get('stream_key')
    shuffle = request.form.get('shuffle') == 'true'
    repeat = request.form.get('repeat') == 'true'
    if not stream_key:
        return jsonify({"status": "error", "message": "مفتاح البث مطلوب."})
    with playlist_lock:
        playlist_control['shuffle'] = shuffle
        playlist_control['repeat'] = repeat
        if not playlist:
            return jsonify({"status": "error", "message": "قائمة التشغيل فارغة."})
        if playlist_running:
            return jsonify({"status": "error", "message": "قائمة التشغيل تعمل حالياً."})
        playlist_running = True
        # make a local copy if repeat is False to consume
        if not repeat:
            # copy to avoid modifying original list passed from UI
            playlist[:] = list(playlist)

    playlist_thread = threading.Thread(target=playlist_runner, args=(stream_key,), daemon=True)
    playlist_thread.start()
    return jsonify({"status": "success", "message": "تم بدء قائمة التشغيل."})


@app.route('/playlist/stop', methods=['POST'])
def playlist_stop():
    global playlist_running, ffmpeg_process
    if ADMIN_PASSWORD and not session.get('logged_in'):
        return jsonify({"status": "error", "message": "غير مصرح لك"})
    playlist_running = False
    try:
        if ffmpeg_process:
            os.killpg(os.getpgid(ffmpeg_process.pid), signal.SIGTERM)
            ffmpeg_process = None
    except Exception:
        pass
    return jsonify({"status": "success", "message": "تم إيقاف قائمة التشغيل/البث."})


@app.route('/playlist/move', methods=['POST'])
def playlist_move():
    if ADMIN_PASSWORD and not session.get('logged_in'):
        return jsonify({"status": "error", "message": "غير مصرح لك"})
    try:
        frm = int(request.form.get('from'))
        to = int(request.form.get('to'))
    except Exception:
        return jsonify({"status": "error", "message": "مؤشرات غير صحيحة."})
    with playlist_lock:
        if frm < 0 or frm >= len(playlist) or to < 0 or to >= len(playlist):
            return jsonify({"status": "error", "message": "مؤشر خارج النطاق."})
        item = playlist.pop(frm)
        playlist.insert(to, item)
    return jsonify({"status": "success", "playlist": playlist})


@app.route('/rename', methods=['POST'])
def rename_video():
    if ADMIN_PASSWORD and not session.get('logged_in'):
        return jsonify({"status": "error", "message": "غير مصرح لك"})
    old = request.form.get('old')
    new = request.form.get('new')
    if not old or not new:
        return jsonify({"status": "error", "message": "أسماء الملفات غير صحيحة."})
    old_path = os.path.join(VIDEO_DIR, old)
    new_path = os.path.join(VIDEO_DIR, new)
    if not os.path.exists(old_path):
        return jsonify({"status": "error", "message": "الملف الأصلي غير موجود."})
    if os.path.exists(new_path):
        return jsonify({"status": "error", "message": "يوجد ملف بنفس الاسم الجديد."})
    try:
        os.rename(old_path, new_path)
        return jsonify({"status": "success", "message": "تمت إعادة التسمية بنجاح."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route('/disk_usage')
def disk_usage():
    total, used, free = shutil.disk_usage('/')
    return jsonify({
        "total_mb": total // (1024 * 1024),
        "used_mb": used // (1024 * 1024),
        "free_mb": free // (1024 * 1024)
    })


@app.route('/stats')
def stats():
    # Attempts to get simple stats via yt-dlp JSON output for a given YouTube URL
    youtube_url = request.args.get('url')
    if not youtube_url:
        return jsonify({"status": "error", "message": "الرابط فارغ."})
    try:
        cmd = f'yt-dlp -j "{youtube_url}"'
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL)
        info = json.loads(out.decode())
        return jsonify({
            "title": info.get('title'),
            "uploader": info.get('uploader'),
            "view_count": info.get('view_count'),
            "like_count": info.get('like_count')
        })
    except Exception as e:
        return jsonify({"status": "error", "message": "فشل جلب الإحصاءات."})


@app.route('/schedule/add', methods=['POST'])
def schedule_add():
    if ADMIN_PASSWORD and not session.get('logged_in'):
        return jsonify({"status": "error", "message": "غير مصرح لك"})
    video_file = request.form.get('video_file')
    start_time = request.form.get('start_time')  # ISO or datetime-local format expected
    # accept duration in hours (can be decimal, e.g., 1.25 for 1 hour 15 minutes)
    try:
        duration_hours = float(request.form.get('duration_hours') or 0)
    except Exception:
        duration_hours = 0
    duration_min = int(duration_hours * 60)
    if not video_file or not start_time:
        return jsonify({"status": "error", "message": "الملف أو التوقيت مفقود."})

    job_id = f"sched_{int(time.time())}"

    def job_start():
        # start the video once (no loop)
        with playlist_lock:
            playlist.clear()
            playlist.append(video_file)
            playlist_control['shuffle'] = False
            playlist_control['repeat'] = False
            global playlist_running, playlist_thread
            playlist_running = True
            playlist_thread = threading.Thread(target=playlist_runner, args=(os.environ.get('SCHEDULE_STREAM_KEY', ''),), daemon=True)
            playlist_thread.start()

    def job_stop():
        global playlist_running, ffmpeg_process
        playlist_running = False
        try:
            if ffmpeg_process:
                os.killpg(os.getpgid(ffmpeg_process.pid), signal.SIGTERM)
        except Exception:
            pass

    try:
        scheduler.add_job(job_start, 'date', run_date=start_time, id=job_id)
        if duration_min > 0:
            try:
                from datetime import datetime, timedelta, timezone
                # parse start_time (expecting ISO format). If no tz info, assume UTC
                dt = datetime.fromisoformat(start_time)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                stop_dt = dt + timedelta(minutes=duration_min)
                scheduler.add_job(job_stop, 'date', run_date=stop_dt.isoformat(), id=f"{job_id}_stop")
            except Exception:
                # if parsing failed, schedule a relative stop in duration minutes
                stop_time = time.time() + duration_min * 60
                scheduler.add_job(job_stop, 'date', run_date=stop_time, id=f"{job_id}_stop")
        return jsonify({"status": "success", "message": "تم جدولة البث."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route('/schedule/list')
def schedule_list():
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({"id": job.id, "next_run_time": str(job.next_run_time)})
    return jsonify({"jobs": jobs})


@app.route('/schedule/remove', methods=['POST'])
def schedule_remove():
    if ADMIN_PASSWORD and not session.get('logged_in'):
        return jsonify({"status": "error", "message": "غير مصرح لك"})
    job_id = request.form.get('job_id')
    try:
        scheduler.remove_job(job_id)
        return jsonify({"status": "success", "message": "تمت إزالة المهمة."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
