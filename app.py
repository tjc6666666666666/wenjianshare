import os
import re
import time
import uuid
import cv2  # 新增：处理视频帧
import requests
from datetime import datetime, timedelta
from PIL import Image
from flask import Flask, render_template, request, redirect, url_for, flash, session, make_response
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from webdav3.client import Client
from functools import wraps
from webdav3.exceptions import WebDavException

# 初始化Flask应用
app = Flask(__name__, instance_relative_config=True)
app.secret_key = 'your-secure-secret-key-here-123456'  # 务必修改为随机密钥
app.config['MAX_CONTENT_LENGTH'] = 1600 * 1024 * 1024  # 1600MB（支持大压缩包）
# Cookie配置（用于普通用户文件归属标识）
COOKIE_NAME = 'file_upload_cookie'  # 固定Cookie键名
COOKIE_EXPIRES = timedelta(days=30)  # Cookie有效期30天

# -------------------------- 1. 配置项（根据实际情况修改） --------------------------
# 数据库配置（SQLite，存储在instance目录）
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///file.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False  # 关闭不必要的警告

# Alist WebDAV配置（原文件存储到Alist）
ALIST_CONFIG = {
    'host': 'http://127.0.0.1:5244/dav/',  # 你的Alist WebDAV地址（必须以/结尾）
    'user': '34',                          # Alist用户名
    'password': '1'                        # Alist密码
}

# 本地缩略图配置
THUMBNAIL_DIR = os.path.join(app.static_folder, 'thumbnails')  # 缩略图存储路径
THUMBNAIL_MAX_SIZE = 200  # 缩略图最大边尺寸（px）- 满足需求

# 允许的文件类型
ALLOWED_EXTENSIONS = {
    'png', 'jpg', 'jpeg', 'gif',  # 图片
    'mp4', 'mov', 'avi',          # 视频
    'zip', '7z', 'rar', 'tar', 'gz'     # 压缩包
}

# 管理员账户配置（固定）
ADMIN_USER = '?'
ADMIN_PWD = '?'

# -------------------------- 2. 初始化组件 --------------------------
# 初始化数据库
db = SQLAlchemy(app)

# 初始化Alist WebDAV客户端
webdav_client = Client({
    'webdav_hostname': ALIST_CONFIG['host'],
    'webdav_login': ALIST_CONFIG['user'],
    'webdav_password': ALIST_CONFIG['password']
})
webdav_client.verify = False  # 忽略SSL验证（本地Alist可开启）
webdav_client.timeout = 30    # 延长超时时间（避免大文件上传超时）

# -------------------------- 3. 数据库模型（无需修改，兼容原有结构） --------------------------
class File(db.Model):
    id = db.Column(db.Integer, primary_key=True)  # 主键
    original_filename = db.Column(db.String(255), nullable=False)  # 原始文件名（支持中文）
    file_type = db.Column(db.String(50), nullable=False)  # 文件类型（image/video/zip等）
    alist_path = db.Column(db.String(512), nullable=False)  # Alist中的存储路径（含中文）
    thumbnail_path = db.Column(db.String(512), nullable=True)  # 本地缩略图路径（图片/视频共用）
    upload_time = db.Column(db.DateTime, default=datetime.utcnow)  # 上传时间（UTC）
    file_size = db.Column(db.Integer, nullable=False)  # 文件大小（字节）
    remark = db.Column(db.Text, nullable=True)  # 文件备注（如压缩包密码）
    upload_cookie = db.Column(db.String(100), nullable=False)  # 新增：上传时的Cookie标识（用于普通用户删除权限）

    def __repr__(self):
        return f'<File {self.original_filename}>'

# 创建数据库表（首次运行自动创建，新增字段需重新生成表）
with app.app_context():
    db.create_all()

# -------------------------- 4. 工具函数（修复视频封面处理函数参数顺序） --------------------------
# 自定义文件名清洗函数（保留中文，过滤不安全字符）
def clean_filename(filename):
    if not filename:
        return ""
    # 保留中文、字母、数字、空格、._-，其他字符替换为下划线
    cleaned = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9\s\._-]', '_', filename)
    cleaned = cleaned.strip()
    # 处理空文件名
    if not cleaned:
        cleaned = datetime.now().strftime('%Y%m%d%H%M%S')
    return cleaned

# 登录装饰器（保护路由）
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session or not session['logged_in']:
            flash('请先登录', 'error')
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# 检查文件类型是否允许
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# 获取文件类型分类（image/video/zip/other）
def get_file_category(filename):
    ext = filename.rsplit('.', 1)[1].lower()
    if ext in ['png', 'jpg', 'jpeg', 'gif']:
        return 'image'
    elif ext in ['mp4', 'mov', 'avi']:
        return 'video'
    elif ext in ['zip', '7z', 'rar', 'tar', 'gz']:
        return 'zip'
    else:
        return 'other'

# 生成按年月日的目录（如2025/09/23）
def get_date_dir():
    now = datetime.now()
    return f'{now.year}/{now.month:02d}/{now.day:02d}'


# 生成图片/视频封面缩略图（最大边200px）
def generate_thumbnail(local_file_path, save_dir):
    try:
        with Image.open(local_file_path) as img:
            img.thumbnail((THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE))  # 固定最大边200px
            filename = clean_filename(os.path.basename(local_file_path))
            # 统一缩略图为JPG格式（避免兼容性问题）
            if not filename.endswith(('.jpg', '.jpeg')):
                filename = os.path.splitext(filename)[0] + '.jpg'
            save_path = os.path.join(save_dir, filename)
            os.makedirs(save_dir, exist_ok=True)
            # 处理透明背景（如PNG）
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            img.save(save_path, 'JPEG', quality=85)
            
            # 关键修复：强制计算相对于static目录的相对路径
            relative_path = os.path.relpath(save_path, app.static_folder)
            return relative_path  # 现在返回的是 "thumbnails/2025/09/23/xxx.jpg" 格式
    except Exception as e:
        app.logger.error(f'生成缩略图失败: {str(e)}')
        return None


# 修复：调整参数顺序，将没有默认值的temp_dir放在前面
def generate_video_cover(source_path, temp_dir, is_video=True):
    """
    生成视频封面
    :param source_path: 视频路径（is_video=True）或用户上传封面路径（is_video=False）
    :param temp_dir: 临时文件目录
    :param is_video: 是否从视频取帧（True=视频第一帧，False=用户上传封面）
    :return: 封面临时路径（JPG），失败返回None
    """
    cover_temp_path = os.path.join(temp_dir, f"cover_{uuid.uuid4().hex}.jpg")
    try:
        if is_video:
            # 从视频读取第一帧
            cap = cv2.VideoCapture(source_path)
            if not cap.isOpened():
                app.logger.error(f"无法打开视频: {source_path}")
                return None
            ret, frame = cap.read()  # 读取第一帧
            cap.release()  # 释放资源
            if not ret:
                app.logger.error(f"无法读取视频第一帧: {source_path}")
                return None
            cv2.imwrite(cover_temp_path, frame)  # 保存为JPG
        else:
            # 用用户上传的封面生成JPG格式
            with Image.open(source_path) as img:
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                img.save(cover_temp_path, 'JPEG', quality=85)
        return cover_temp_path
    except Exception as e:
        app.logger.error(f"生成视频封面失败: {str(e)}")
        if os.path.exists(cover_temp_path):
            os.remove(cover_temp_path)
        return None

# 递归创建Alist多级目录
def create_alist_dir(webdav_client, target_dir):
    try:
        dir_levels = []
        current_dir = ""
        for dir_name in target_dir.split('/'):
            if not dir_name:
                continue
            current_dir = f"{current_dir}/{dir_name}" if current_dir else dir_name
            dir_levels.append(current_dir)
        
        for dir_path in dir_levels:
            if not webdav_client.check(dir_path):
                webdav_client.mkdir(dir_path)
                app.logger.info(f'Alist目录创建成功: {dir_path}')
            else:
                app.logger.info(f'Alist目录已存在: {dir_path}')
        
        return True
    except WebDavException as e:
        app.logger.error(f'创建Alist目录失败（{target_dir}）: {str(e)}')
        flash(f'创建存储目录失败: {str(e)}', 'error')
        return False
    except Exception as e:
        app.logger.error(f'创建Alist目录时发生未知错误: {str(e)}')
        flash(f'创建存储目录时发生未知错误: {str(e)}', 'error')
        return False

# 获取Alist文件的最终网盘链接
def get_cloud_disk_url(alist_file_path):
    try:
        alist_temp_url = f"{ALIST_CONFIG['host']}{alist_file_path}"
        with requests.Session() as s:
            s.auth = (ALIST_CONFIG['user'], ALIST_CONFIG['password'])
            response = s.get(alist_temp_url, allow_redirects=True, stream=True, timeout=15)
            response.close()
            if response.status_code in [200, 301, 302]:
                return response.url
            return None
    except Exception as e:
        app.logger.error(f'获取网盘链接失败: {str(e)}')
        return None

# 获取当前用户的上传Cookie（无则生成并设置）
def get_or_set_upload_cookie():
    # 从请求中获取现有Cookie
    upload_cookie = request.cookies.get(COOKIE_NAME)
    if not upload_cookie:
        # 生成唯一Cookie值（UUID）
        upload_cookie = str(uuid.uuid4().hex)
    return upload_cookie

# -------------------------- 5. 路由功能（修改上传逻辑，新增视频封面处理） --------------------------
# 登录路由（新增管理员身份识别）
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        # 验证管理员账户
        if username == ADMIN_USER and password == ADMIN_PWD:
            session['logged_in'] = True
            session['is_admin'] = True  # 标记为管理员
            session['username'] = ADMIN_USER
            next_page = request.args.get('next', url_for('index'))
            flash('管理员登录成功', 'success')
            return redirect(next_page)
        # 验证普通用户（原账户）
        elif username == '123' and password == '123':
            session['logged_in'] = True
            session['is_admin'] = False  # 标记为普通用户
            session['username'] = username
            next_page = request.args.get('next', url_for('index'))
            flash('普通用户登录成功', 'success')
            return redirect(next_page)
        else:
            flash('用户名或密码错误', 'error')
    
    return render_template('login.html')

# 登出路由
@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('is_admin', None)
    session.pop('username', None)
    flash('已成功登出', 'success')
    return redirect(url_for('login'))

# 首页（文件列表+分页，显示复选框/删除按钮）
@app.route('/')
@login_required
def index():
    # 获取当前页码
    page = request.args.get('page', 1, type=int)
    # 分页查询
    pagination = File.query.order_by(File.upload_time.desc()).paginate(
        page=page, per_page=50, error_out=False
    )
    files = pagination.items
    # 获取当前用户的上传Cookie（用于普通用户权限判断）
    current_cookie = request.cookies.get(COOKIE_NAME)
    return render_template('index.html', 
                           files=files, 
                           pagination=pagination,
                           is_admin=session.get('is_admin', False),
                           current_cookie=current_cookie)

# 文件上传路由（新增视频封面处理+Cookie记录）
@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    if 'files' not in request.files:
        flash('未选择任何文件', 'error')
        return redirect(url_for('index'))
    
    files = request.files.getlist('files')
    remarks = request.form.getlist('remarks')
    cover_files = request.files.getlist('cover_files')  # 新增：接收视频封面文件
    date_dir = get_date_dir()
    # 获取/生成上传Cookie
    upload_cookie = get_or_set_upload_cookie()
    
    # 创建Alist目录
    if not create_alist_dir(webdav_client, date_dir):
        return redirect(url_for('index'))
    
    # 处理文件上传
    response = make_response()
    for idx, file in enumerate(files):
        if not file or not file.filename:
            continue
        
        original_filename = file.filename
        cleaned_filename = clean_filename(original_filename)
        
        if not allowed_file(original_filename):
            flash(f'文件 {original_filename} 类型不允许', 'error')
            continue
        
        file_remark = remarks[idx].strip() if idx < len(remarks) else None
        file_category = get_file_category(original_filename)
        thumbnail_path = None  # 图片/视频共用此字段
        
        try:
            # 1. 保存原始文件到临时目录
            temp_dir = os.path.join(app.root_path, 'temp')
            os.makedirs(temp_dir, exist_ok=True)
            temp_file_path = os.path.join(temp_dir, cleaned_filename)
            file.save(temp_file_path)
            file_size = os.path.getsize(temp_file_path)
            
            # 2. 处理视频封面（图片直接用原文件生成缩略图）
            cover_temp_path = None
            if file_category == 'video':
                # 获取当前视频对应的封面文件（用户可选）
                cover_file = cover_files[idx] if idx < len(cover_files) else None
                if cover_file and cover_file.filename:
                    # 用用户上传的封面
                    user_cover_path = os.path.join(temp_dir, f"user_cover_{cleaned_filename}")
                    cover_file.save(user_cover_path)
                    # 修复：调整参数顺序，将temp_dir作为第二个参数传入
                    cover_temp_path = generate_video_cover(
                        source_path=user_cover_path, 
                        temp_dir=temp_dir,
                        is_video=False
                    )
                    # 清理用户上传的原始封面
                    if os.path.exists(user_cover_path):
                        os.remove(user_cover_path)
                else:
                    # 无封面，用视频第一帧
                    # 修复：调整参数顺序，将temp_dir作为第二个参数传入
                    cover_temp_path = generate_video_cover(
                        source_path=temp_file_path, 
                        temp_dir=temp_dir,
                        is_video=True
                    )
            
            # 3. 生成缩略图（图片/视频统一处理，最大边200px）
            if file_category == 'image':
                thumbnail_save_dir = os.path.join(THUMBNAIL_DIR, date_dir)
                thumbnail_path = generate_thumbnail(temp_file_path, thumbnail_save_dir)
            elif file_category == 'video' and cover_temp_path:
                thumbnail_save_dir = os.path.join(THUMBNAIL_DIR, date_dir)
                thumbnail_path = generate_thumbnail(cover_temp_path, thumbnail_save_dir)
                # 清理封面临时文件
                if os.path.exists(cover_temp_path):
                    os.remove(cover_temp_path)
            
            # 4. 上传原始文件到Alist
            alist_file_path = f"{date_dir}/{cleaned_filename}"
            webdav_client.upload(remote_path=alist_file_path, local_path=temp_file_path)
            app.logger.info(f'文件上传Alist成功: {alist_file_path}')
            
            # 5. 保存到数据库（复用thumbnail_path字段，兼容原有结构）
            new_file = File(
                original_filename=original_filename,
                file_type=file_category,
                alist_path=alist_file_path,
                thumbnail_path=thumbnail_path,  # 视频封面缩略图路径
                file_size=file_size,
                remark=file_remark,
                upload_cookie=upload_cookie  # 记录上传Cookie
            )
            db.session.add(new_file)
            db.session.commit()
            
            flash(f'文件 {original_filename} 上传成功', 'success')
        except WebDavException as e:
            db.session.rollback()
            flash(f'文件 {original_filename} 上传到Alist失败: {str(e)}', 'error')
            app.logger.error(f'文件 {original_filename} 上传Alist失败: {str(e)}')
        except Exception as e:
            db.session.rollback()
            flash(f'文件 {original_filename} 处理失败: {str(e)}', 'error')
            app.logger.error(f'文件 {original_filename} 处理失败: {str(e)}')
        finally:
            # 清理原始文件临时文件
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
    
    # 设置上传Cookie（有效期30天）
    response.set_cookie(COOKIE_NAME, upload_cookie, expires=datetime.now() + COOKIE_EXPIRES)
    response.headers['Location'] = url_for('index')
    response.status_code = 302
    return response

# 重定向到网盘链接
@app.route('/file/<int:file_id>')
@login_required
def redirect_to_cloud(file_id):
    file = File.query.get_or_404(file_id)
    cloud_url = get_cloud_disk_url(file.alist_path)
    if cloud_url:
        return redirect(cloud_url, code=302)
    flash('无法获取有效的网盘链接', 'error')
    return redirect(url_for('index'))

# 新增：文件删除路由（支持批量删除+权限控制）
@app.route('/delete-files', methods=['POST'])
@login_required
def delete_files():
    # 获取选中的文件ID列表
    file_ids = request.form.getlist('file_ids', type=int)
    if not file_ids:
        flash('未选择任何文件', 'error')
        return redirect(url_for('index'))
    
    # 获取当前用户信息
    is_admin = session.get('is_admin', False)
    current_cookie = request.cookies.get(COOKIE_NAME)
    deleted_count = 0
    
    try:
        for file_id in file_ids:
            file = File.query.get(file_id)
            if not file:
                flash(f'文件ID {file_id} 不存在，跳过删除', 'warning')
                continue
            
            # 权限判断
            if is_admin:
                # 管理员：无限制删除
                pass
            else:
                # 普通用户：仅允许删除自己Cookie上传的文件
                if file.upload_cookie != current_cookie:
                    flash(f'无权限删除文件 {file.original_filename}（非本设备上传）', 'error')
                    continue
            
            # 1. 删除Alist上的文件
            if webdav_client.check(file.alist_path):
                webdav_client.clean(file.alist_path)
                app.logger.info(f'Alist文件删除成功: {file.alist_path}')
            
            # 2. 删除本地缩略图
            if file.thumbnail_path:
                thumbnail_full_path = os.path.join(app.static_folder, file.thumbnail_path)
                if os.path.exists(thumbnail_full_path):
                    os.remove(thumbnail_full_path)
                    app.logger.info(f'本地缩略图删除成功: {thumbnail_full_path}')
            
            # 3. 删除数据库记录
            db.session.delete(file)
            deleted_count += 1
        
        db.session.commit()
        flash(f'成功删除 {deleted_count} 个文件', 'success')
    except WebDavException as e:
        db.session.rollback()
        flash(f'删除Alist文件失败: {str(e)}', 'error')
        app.logger.error(f'删除Alist文件失败: {str(e)}')
    except Exception as e:
        db.session.rollback()
        flash(f'删除文件时发生未知错误: {str(e)}', 'error')
        app.logger.error(f'删除文件时发生未知错误: {str(e)}')
    
    return redirect(url_for('index'))

# 新增：普通用户单个文件删除路由（可选，前端也可通过批量删除实现）
@app.route('/delete-file/<int:file_id>', methods=['POST'])
@login_required
def delete_single_file(file_id):
    file = File.query.get_or_404(file_id)
    is_admin = session.get('is_admin', False)
    current_cookie = request.cookies.get(COOKIE_NAME)
    
    # 权限判断
    if not is_admin and file.upload_cookie != current_cookie:
        flash(f'无权限删除文件 {file.original_filename}（非本设备上传）', 'error')
        return redirect(url_for('index'))
    
    try:
        # 删除Alist文件
        if webdav_client.check(file.alist_path):
            webdav_client.clean(file.alist_path)
        
        # 删除本地缩略图
        if file.thumbnail_path:
            thumbnail_full_path = os.path.join(app.static_folder, file.thumbnail_path)
            if os.path.exists(thumbnail_full_path):
                os.remove(thumbnail_full_path)
        
        # 删除数据库记录
        db.session.delete(file)
        db.session.commit()
        flash(f'文件 {file.original_filename} 删除成功', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'删除文件失败: {str(e)}', 'error')
    
    return redirect(url_for('index'))

if __name__ == '__main__':
    # 确保临时目录和缩略图目录存在
    os.makedirs(os.path.join(app.root_path, 'temp'), exist_ok=True)
    os.makedirs(THUMBNAIL_DIR, exist_ok=True)
    # 运行服务（允许外部访问）
    app.run(debug=False, host="0.0.0.0", port=5678)
