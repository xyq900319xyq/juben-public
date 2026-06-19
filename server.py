"""
Storyboard App - 剧本分镜助手 (v4)
项目 + 剧集管理 | SQLite 持久化 | 只做分镜
"""
import subprocess, json, os, re, uuid, sqlite3, shutil, requests, time, threading, hashlib, hmac
from datetime import datetime

_LICENSE_SECRET = b"jub" + b"en-secret-key-2026"

def _verify_license():
    try:
        lp = os.path.join(os.environ.get("USER_DATA") or os.path.join(os.environ.get("APPDATA", os.path.dirname(__file__)), "Juben"), "license.dat")
        if not os.path.exists(lp): return False
        with open(lp) as f:
            code = f.read().strip()
        # 格式验证
        c = code.replace("-","").upper().replace("JB","")
        if len(c) != 16: return False
        pfx, csm = c[:8], c[8:]
        if csm != hmac.new(_LICENSE_SECRET, pfx.encode(), hashlib.sha256).hexdigest()[:8].upper():
            return False
        # 联网白名单检查
        import urllib.request as _ur
        r = _ur.urlopen("https://raw.githubusercontent.com/xyq900319xyq/juben-public/main/codes.txt", timeout=8)
        for line in r.read().decode().split('\n'):
            if line.strip() == code:
                return True
        return False
    except: return False

if not _verify_license():
    import logging
    logging.warning("License check failed, but launcher handles activation")
import logging
os.makedirs(os.path.dirname('/tmp/hermes_server.log'), exist_ok=True)
logging.basicConfig(
    filename='/tmp/hermes_server.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

from flask import Flask, request, jsonify, render_template, send_file

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['TEMPLATES_AUTO_RELOAD'] = True  # 禁用模板缓存

_data_dir = os.environ.get("USER_DATA") or os.path.join(os.environ.get("APPDATA", os.path.dirname(__file__)), "Juben")
DB_PATH = os.path.join(_data_dir, "projects.db")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "outputs")


def _output_path(project_name: str, episode_num: int, title: str) -> str:
    safe_project = re.sub(r'[\\/*?:"<>|]', '_', project_name)
    safe_title = re.sub(r'[\\/*?:"<>|]', '_', title.split('：')[0].split(':')[0].strip())
    if not safe_title:
        safe_title = f"第{episode_num}集"
    dir_path = os.path.join(OUTPUT_DIR, safe_project)
    os.makedirs(dir_path, exist_ok=True)
    filename = f"Ep{episode_num:02d}_{safe_title}_分镜.md"
    return os.path.join(dir_path, filename)


def save_to_disk(project_name: str, episode_num: int, title: str, storyboard: str):
    if storyboard:
        path = _output_path(project_name, episode_num, title)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(storyboard)


def save_all_to_disk():
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT e.episode_num, e.title, e.storyboard, p.name as project_name
            FROM episodes e JOIN projects p ON e.project_id = p.id
            WHERE e.status = 'completed' AND e.storyboard != ''
        """).fetchall()
        conn.close()
        count = 0
        for r in rows:
            path = _output_path(r["project_name"], r["episode_num"], r["title"])
            if not os.path.exists(path):
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(r["storyboard"])
                count += 1
        if count:
            print(f"  [auto-save] {count} 个剧集已同步到磁盘 outputs/")
    except Exception as e:
        print(f"  [auto-save] 同步出错: {e}")


def delete_from_disk(project_name: str, episode_num: int, title: str):
    path = _output_path(project_name, episode_num, title)
    if os.path.exists(path):
        os.remove(path)


# ====== 数据库 ======

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT DEFAULT '',
            asset_cache TEXT DEFAULT '',
            style_id TEXT DEFAULT '',
            render_type TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name ON projects(name);
        CREATE TABLE IF NOT EXISTS episodes (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            episode_num INTEGER NOT NULL,
            title TEXT DEFAULT '',
            script TEXT NOT NULL,
            storyboard TEXT DEFAULT '',
            style_id TEXT DEFAULT '',
            render_type TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            UNIQUE(project_id, episode_num)
        );
        CREATE TABLE IF NOT EXISTS audio_selections (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            asset TEXT NOT NULL,
            audio_file TEXT NOT NULL,
            UNIQUE(project, asset)
        );
    """)
    conn.commit()
    conn.close()


init_db()

# 安全迁移：加 prompt 列（如果不存在）
def _safe_migrate():
    conn = get_db()
    existing = {r[1] for r in conn.execute("PRAGMA table_info(episodes)").fetchall()}
    if "prompt" not in existing:
        conn.execute("ALTER TABLE episodes ADD COLUMN prompt TEXT DEFAULT ''")
    if "prompt_status" not in existing:
        conn.execute("ALTER TABLE episodes ADD COLUMN prompt_status TEXT DEFAULT ''")
    conn.commit()
    conn.close()

_safe_migrate()

# ====== 视觉风格 ======

STYLES = {
    "classic-cinematic": {"name": "经典电影", "en": "Classic Cinematic", "guidance": "经典好莱坞叙事风格，深景深构图，三点布光，轨道平稳移动，暖色温，端庄华丽的画面。"},
    "film-noir": {"name": "黑色电影", "en": "Film Noir", "guidance": "高对比度黑白或低饱和度画面，大面积阴影，百叶窗条纹光，烟雾缭绕。倾斜构图制造不安感。"},
    "epic-blockbuster": {"name": "史诗大片", "en": "Epic Blockbuster", "guidance": "大景别全景航拍，壮观的自然光或金色时刻光线，慢动作升格，恢弘配乐感画面。"},
    "intimate-drama": {"name": "亲密剧情", "en": "Intimate Drama", "guidance": "浅景深特写，自然柔光，手持轻微晃动增加亲密感。焦点紧跟人物面部微表情，暖色调。"},
    "romantic-film": {"name": "浪漫爱情", "en": "Romantic Film", "guidance": "柔光镜效果，暖金色光线，浅景深虚化背景，粉色调。慢动作、逆光剪影、镜头光晕。"},
    "documentary-raw": {"name": "纪实手持", "en": "Raw Documentary", "guidance": "手持摄影的轻微晃动，完全自然光，焦点偶尔偏移增加真实感。跟焦跟随人物运动。"},
    "news-report": {"name": "新闻纪实", "en": "News Report", "guidance": "固定机位或平稳肩扛，标准镜头（35-50mm），正面布光。冷静客观的视角，干净清晰的构图。"},
    "cyberpunk-neon": {"name": "赛博朋克", "en": "Cyberpunk Neon", "guidance": "霓虹紫红与冰蓝同框，轮廓光把人物从暗色背景中剥离。浅景深将霓虹灯化为迷幻光斑。"},
    "wuxia-classic": {"name": "古典武侠", "en": "Classic Wuxia", "guidance": "山间薄雾与落叶营造江湖苍茫感。摇臂从高处缓缓降至人物。自然侧光模拟竹林斑驳光影。"},
    "horror-thriller": {"name": "恐怖惊悚", "en": "Horror Thriller", "guidance": "低角度仰拍制造压迫感，暗角画面，蓝绿冷色调。突然的跳切和快速摇镜制造惊吓。"},
    "music-video": {"name": "MV风格", "en": "Music Video", "guidance": "快节奏剪辑，频闪灯光效果，超广角变形，色彩高度饱和或高对比。升格慢动作与降格快放。"},
    "family-warmth": {"name": "家庭温情", "en": "Family Warmth", "guidance": "温暖柔和的黄昏光线，浅景深突出人物互动。固定或缓慢推轨，轻微暖色调，画面干净明亮。"},
    "action-intense": {"name": "动作激烈", "en": "Intense Action", "guidance": "快速手持跟拍，急促变焦推拉，多角度快速切换。低角度仰拍强化力量感，碎片飞溅。"},
    "suspense-mystery": {"name": "悬疑推理", "en": "Suspense Mystery", "guidance": "低照度环境，指向性光源（手电、台灯），大量特写和插入镜头。浅景深限制观众视野。"},
    "hk-retro-90s": {"name": "90s港片", "en": "90s Hong Kong", "guidance": "偏黄绿色调，柔光镜或轻微过曝，手持跟拍。快速变焦推拉、倾斜构图制造动感。"},
    "golden-age-hollywood": {"name": "好莱坞黄金时代", "en": "Golden Age Hollywood", "guidance": "完美三点布光消除一切不美的阴影，深景深精心构图。轨道缓慢优雅移动如华尔兹。"},
}

RENDER_TYPES = {
    # === 3D动画 ===
    "3d_xuanhuan": {"name": "3D玄幻", "category": "3D动画", "guidance": "东方玄幻3D风格。仙气飘渺氛围，粒子光效丰富，法术特效华丽璀璨。场景宏大壮阔，角色身姿飘逸。色彩以金、紫、青为主调，光影通透富有层次感。画面需有CG动画电影的精致度。"},
    "3d_american": {"name": "3D美式", "category": "3D动画", "guidance": "美式3D动画风格（皮克斯/迪士尼质感）。夸张的肢体动作和生动表情，鲜明的色彩对比，角色造型风格化。画面干净明亮，适合全年龄段审美。"},
    "3d_q_version": {"name": "3DQ版", "category": "3D动画", "guidance": "3D Q版可爱风格。大头小身体比例（约1:2头身），圆润造型无锐角。明亮活泼的糖果色系，可爱的表情和夸张的动作设计。场景道具同样圆润化处理。"},
    "3d_realistic": {"name": "3D写实", "category": "3D动画", "guidance": "3D超写实风格。逼真材质纹理（PBR渲染），接近真人比例和皮肤质感。物理级光照模拟，电影级景深和运动模糊。追求以假乱真的画面效果。"},
    "3d_block": {"name": "3D块面", "category": "3D动画", "guidance": "3D低多边形几何风格。角色和场景由多面体构成，强调硬朗的体块感和结构性。色彩使用扁平化纯色块，光影以面为单位，类似几何雕塑的3D化呈现。"},
    "3d_voxel": {"name": "3D方块世界", "category": "3D动画", "guidance": "3D方块像素世界风格（Minecraft质感）。所有元素由小立方体构成，像素化的纹理贴图。角色、建筑、植被均为方块造型，充满手工搭建的童趣感。"},
    "3d_mobile": {"name": "3D手游", "category": "3D动画", "guidance": "3D手游画面风格。为移动端优化的卡通渲染，简洁的模型面和贴图精度。色彩明快饱和度高，角色造型偏向日系/韩系手游美术，特效华丽但不复杂。"},
    "3d_render_2d": {"name": "3D渲染2D", "category": "3D动画", "guidance": "三渲二技术（3D建模2D渲染）。保留3D的立体透视和运镜自由度，但渲染成2D动画的平面绘画质感。色彩分层明确，光影简化，类似高预算2D动画电影。"},
    "jp_3d_render_2d": {"name": "日式3D渲染2D", "category": "3D动画", "guidance": "日式三渲二风格。动漫赛璐珞着色，清晰的黑色勾线轮廓，色彩填充扁平化。光影以二分法为主（亮面/暗面），保留日系动画的手绘感但具备3D的空间纵深。"},
    # === 2D动画 ===
    "2d_animation": {"name": "2D动画", "category": "2D动画", "guidance": "传统手绘2D动画。流畅的逐帧动画质感，自然的线条变化，平面化分层上色。色彩柔和过渡，画面有温度的手工绘画痕迹。"},
    "2d_movie": {"name": "2D电影", "category": "2D动画", "guidance": "2D电影级动画。精致的场景绘制和细节刻画，电影级构图和光影设计。类似吉卜力/迪士尼手绘动画长片的质量，色彩丰富且和谐，画面有油画般的厚重感。"},
    "2d_fantasy": {"name": "2D奇幻动画", "category": "2D动画", "guidance": "2D奇幻风格。魔法元素、奇异生物和壮丽场景。色彩偏向紫、蓝、金色的幻彩组合，有发光和粒子效果。画面充满想象力和神秘氛围。"},
    "2d_retro": {"name": "2D复古动画", "category": "2D动画", "guidance": "2D复古怀旧动画。80-90年代电视动画质感，赛璐珞胶片上色风格，略微褪色的暖色调。画面有轻微的颗粒感和胶片特有的柔和质感，唤起童年回忆。"},
    "2d_american": {"name": "2D美式动画", "category": "2D动画", "guidance": "美式2D卡通风格。夸张变形的人物造型，大胆奔放的线条，高饱和度色彩。动作弹性十足（squash & stretch），表情极度夸张，喜剧感强。"},
    "2d_ghibli": {"name": "2D吉卜力", "category": "2D动画", "guidance": "吉卜力工作室风格。细腻的水彩手绘背景，柔和的自然光线，温暖治愈的整体氛围。角色动作真实细腻，场景充满生活气息和细节，天空和绿植刻画尤其精美。色彩偏向柔和的自然色系。"},
    "2d_retro_girl": {"name": "2D复古少女", "category": "2D动画", "guidance": "复古少女漫画风格。星星眼、飘逸长发、华丽繁复的服饰。粉嫩梦幻色调，大量花卉、星星、闪光等装饰元素。纤细优雅的线条，浪漫柔美的整体氛围。"},
    "2d_korean": {"name": "2D韩式动画", "category": "2D动画", "guidance": "韩式动画/webtoon风格。精致的人物设计注重时尚感和美型度，干净的线条和配色。肤色偏白皙，五官精致，发型服饰紧跟潮流。画面通透明亮，高级灰调配色。"},
    "2d_shonen": {"name": "2D热血动画", "category": "2D动画", "guidance": "热血少年动画风格。激烈战斗场面，速度线和冲击波效果丰富。爆炸和烟尘特效大量运用，角色表情夸张充满张力。色彩高饱和，对比强烈，画面充满爆发力。"},
    "2d_akira": {"name": "2D鸟山明", "category": "2D动画", "guidance": "鸟山明漫画风格。圆润饱满的角色造型，Q弹的肢体比例，简洁有力的线条。头发造型夸张呈锯齿状，机械设计精巧（胶囊公司风格）。场景有广阔的荒野和科幻元素。"},
    "2d_doraemon": {"name": "2D哆啦A梦", "category": "2D动画", "guidance": "哆啦A梦/藤子不二雄风格。圆润可爱的角色设计，简洁明快的线条和色彩。日常温馨的日式街道和家庭场景，蓝天白云的明亮色调。画面充满童趣和温暖。"},
    "2d_fujimoto": {"name": "2D藤本树", "category": "2D动画", "guidance": "藤本树（链锯人/炎拳）风格。电影感分镜构图，写实的表情刻画和肢体动作。粗犷有力的线条，大量阴影和留白对比。独特的叙事节奏和视觉冲击力，画面有粗粝质感。"},
    "2d_mob": {"name": "2D灵能百分百", "category": "2D动画", "guidance": "灵能百分百（ONE）风格。简约甚至粗糙的人物线条，但爆发场景时极致华丽的作画。强烈的反差感：平时朴素的画风在超能力发动时转变为绚丽的特效作画。色彩在爆发时极度饱和。"},
    "2d_jojo": {"name": "2D JOJO风", "category": "2D动画", "guidance": "JOJO的奇妙冒险风格。夸张扭曲的人物pose（JOJO立），强烈的黑色粗轮廓线，独特的时装设计感配色。经常使用高对比度的异色搭配，画面充满时尚感和戏剧性。"},
    "2d_detective": {"name": "2D日式侦探", "category": "2D动画", "guidance": "日式侦探/悬疑风格。阴郁沉重的氛围，硬朗写实的线条。大量阴影和暗部处理，有限光源（窗户光、台灯）。色调偏冷灰和深蓝，画面有强烈的悬疑感和压迫感。"},
    "2d_slamdunk": {"name": "2D灌篮高手", "category": "2D动画", "guidance": "灌篮高手（井上雄彦）风格。写实的运动描绘和人体结构，充满力量感的动态姿势。细腻的汗水和肌肉刻画，篮球场光影真实。90年代日本动画的经典质感，偏写实人物比例。"},
    "2d_astroboy": {"name": "2D手冢治虫", "category": "2D动画", "guidance": "手冢治虫经典漫画风格。圆润可爱的大眼睛角色，简洁优雅的线条，黑白为主但有层次。复古的未来主义设计，经典的日本漫画始祖画风，有温度的手绘质感。"},
    "2d_deathnote": {"name": "2D死亡笔记", "category": "2D动画", "guidance": "死亡笔记风格。暗黑哥特美学，精细繁复的阴影排线。灰黑主色调，偶尔的红色点缀。人物瘦削修长，眼神锐利。画面充满压抑感和心理战的紧张氛围。"},
    "2d_thick_line": {"name": "2D粗线条", "category": "2D动画", "guidance": "粗线条卡通风格。醒目的加粗轮廓线（2-3倍常规粗细），强烈的视觉冲击力。色彩填充简单直接，阴影用硬边黑色块。类似成人向卡通或街头涂鸦风格。"},
    "2d_rubberhose": {"name": "2D橡皮管动画", "category": "2D动画", "guidance": "1930年代橡皮管动画风格。角色四肢如橡皮管般弹性弯曲，没有关节限制。黑白或复古棕褐色调，画面有老电影的噪点和划痕质感。动作夸张滑稽，充满早期动画的纯真趣味。"},
    "2d_q_version": {"name": "2DQ版", "category": "2D动画", "guidance": "2D Q版可爱风格。大眼睛小嘴巴的萌系角色，圆润饱满的短小身体。粉嫩明亮的配色，大量爱心、星星等可爱装饰元素。画面充满治愈和欢乐感。"},
    "2d_pixel": {"name": "2D像素", "category": "2D动画", "guidance": "像素艺术风格。低分辨率复古游戏画面质感，8bit/16bit时代的美学。马赛克化的角色和场景，有限的色盘（尤其是16色或256色）。画面有怀旧电子游戏的感觉。"},
    "2d_gongbi": {"name": "2D工笔风", "category": "2D动画", "guidance": "中国传统工笔画风格。精致细腻的线条勾勒，层层渲染的色彩过渡。典雅的中国古典配色（朱砂、石青、藤黄），绢本或宣纸质感。画面有中国传统美学的端庄和雅致。"},
    "2d_stick": {"name": "2D简笔画", "category": "2D动画", "guidance": "极简简笔画风格。最少的线条表达人物和场景，火柴人级别的简约。但通过巧妙的动态设计和微表情让画面生动有趣。色彩极简，通常只用2-3种颜色。"},
    "2d_watercolor": {"name": "2D水彩", "category": "2D动画", "guidance": "水彩晕染风格。柔和的色彩边界，自然的颜料渗透和渐变效果。透明的色彩叠加，纸张纹理可见。画面有艺术插画的精致感，色彩清新淡雅，留白恰到好处。"},
    "2d_simple_line": {"name": "2D简单线条", "category": "2D动画", "guidance": "极简单线条风格。只有干净的轮廓线条，少量或没有色彩填充。依赖线条的粗细变化表达形体，留白面积大。画面优雅简洁，类似插画式的极简表达。"},
    "2d_comic": {"name": "2D美式漫画", "category": "2D动画", "guidance": "美式超级英雄漫画风格。网点纸阴影，爆炸状对话框和拟声词。高对比度的上色，肌肉线条分明。画面充满戏剧张力和动作感，典型的Marvel/DC漫画视觉。"},
    "2d_shoujo": {"name": "2D少女漫画", "category": "2D动画", "guidance": "日式少女漫画风格。纤细优美的线条，大量花卉和闪亮网点效果。人物身材修长（8-9头身），发型华丽飘逸。浪漫的构图方式，画面充满粉色泡泡般的梦幻氛围。"},
    "2d_horror": {"name": "2D诡异惊悚", "category": "2D动画", "guidance": "诡异惊悚风格（伊藤润二式）。扭曲变形的角色造型，密集繁复的线条排布。黑白色调为主，大量阴影和暗部。画面制造深层的不安和恐惧，日常场景中侵入异常元素。细节过度描写强化恐怖感。"},
    # === 真人影视 ===
    "real_movie": {"name": "真人电影", "category": "真人影视", "guidance": "真人电影质感。真实的人类演员和物理场景，电影级摄影构图和调色。自然的皮肤纹理和环境光，注意避免CG感。画面有电影胶片的色彩科学和动态范围。"},
    "real_costume": {"name": "真人古装", "category": "真人影视", "guidance": "真人古装影视风格。真实考究的古代服饰（汉服等）和道具，古建筑实景或精致搭景。服化道细节丰富，注重历史质感和年代氛围。古典中式美学色彩搭配。"},
    "real_hk_retro": {"name": "真人复古港片", "category": "真人影视", "guidance": "90年代香港电影风格。偏黄绿色调的画面，略微的柔光或过曝效果。胶片颗粒感，手持摄影的临场感。港式动作片的运镜节奏和构图。"},
    "real_wuxia": {"name": "真人复古武侠", "category": "真人影视", "guidance": "真人武侠影视风格。真实的武打动作和江湖场景，竹林天际、大漠客栈等武侠标志性景观。自然的微风和衣袂飘动，武侠世界的真实质感和意境。"},
    "real_bloom": {"name": "真实光晕", "category": "真人影视", "guidance": "光晕美学风格。柔和的镜头光晕和逆光拍摄效果，梦幻的画面氛围。暖金色光线透过树叶或窗纱的斑驳感。画面有朦胧的美感和浪漫气息。"},
    # === 定格动画 ===
    "stop_motion": {"name": "定格动画", "category": "定格动画", "guidance": "经典定格动画风格。逐帧拍摄的实体模型，微妙的帧间抖动感和手工操作痕迹。实体材质的光影质感（非CG的完美平滑），有温度和工匠精神的手工艺术美感。"},
    "figure_stop_motion": {"name": "手办定格动画", "category": "定格动画", "guidance": "手办/可动人偶定格动画。使用Action Figure和场景模型逐帧拍摄。关节可动范围的限制感也是特色，手办的涂装质感可见。适合超级英雄或机甲类题材。"},
    "clay_stop_motion": {"name": "粘土定格动画", "category": "定格动画", "guidance": "粘土/橡皮泥定格动画。柔软可变形的角色材质，手工捏制的痕迹和指纹。形态可以逐帧变形（transform），色彩鲜艳的橡皮泥质感。充满童趣和创意的手工艺术。"},
    "lego_stop_motion": {"name": "积木定格动画", "category": "定格动画", "guidance": "乐高/积木定格动画。标准的乐高小人仔和积木构建的场景。方块化的世界，乐高特有的卡扣结构。色彩鲜明块状分明，有玩具世界的独特魅力和趣味性。"},
    "felt_stop_motion": {"name": "毛绒定格动画", "category": "定格动画", "guidance": "毛绒布偶定格动画。柔软的毛绒布料质感，温暖可爱的布偶角色。布料纹理和缝线可见，动作幅度受布料限制而显得笨拙可爱。整体色调柔和温暖，适合低幼或治愈题材。"},
}

# ====== 任务管理 ======

tasks = {}
tasks_lock = threading.Lock()
generating = set()
generating_lock = threading.Lock()


def build_storyboard_prompt(script: str, previous_summaries: list = None,
                            style_id: str = None, render_type: str = None) -> str:
    context_block = ""
    if previous_summaries:
        context_block = "【前序剧集摘要 - 请保持连贯性】\n"
        for s in previous_summaries:
            context_block += f"第{s['episode_num']}集「{s['title']}」剧情概要：{s['summary']}\n"
        context_block += "\n"

    style_block = ""
    if style_id and style_id in STYLES:
        s = STYLES[style_id]
        style_block = f"""【视觉风格指定】
使用「{s['name']}」({s['en']})视觉风格：
{s['guidance']}
请在分镜中严格遵循此视觉风格的构图、光影、色调和运镜方式。

"""

    render_block = ""
    if render_type and render_type in RENDER_TYPES:
        r = RENDER_TYPES[render_type]
        guidance_text = r.get("guidance", "")
        render_block = f"""【渲染类型指定】
使用「{r['name']}」({r['category']})渲染风格。
视觉特征：{guidance_text}
请在分镜的画面描述中严格遵循以上渲染类型的视觉特征进行描绘，确保每一个镜头的画面描述都与该渲染风格一致。
"""

    return f"""【重要：必须全部使用中文输出，包括所有术语和描述】
【关键：直接在回复中输出完整分镜脚本文字，禁止使用 write_file 或其他工具写入文件。分镜内容必须出现在你的回复正文中。】

{render_block}{style_block}请为以下剧本生成完整的分镜脚本。{"注意：这是续集，请严格参照前序剧集的人物、世界观、剧情伏笔，确保连贯。" if previous_summaries else ""}

{context_block}
【本次剧本】
{script}

请按以下格式直接在回复中输出分镜（全部使用中文，不要用工具写文件）：
以 "## 🎬 导演核算报告" 开始，然后是 "# 第X集：标题 — 完整分镜脚本"
1. 每个镜头包含：镜头号、景别、时长、画面描述、运镜方式、对白/旁白
2. 整体风格说明
3. 镜头总数统计
4. 如为续集，在开头说明与前序剧集的衔接点

再次强调：所有输出必须使用中文，直接在回复正文中输出，不要写入文件。"""


def summarize_storyboard(storyboard_text: str) -> str:
    """从分镜结果中提取剧情摘要"""
    markers = [
        "\n## 分镜脚本",
        "\n## 完整分镜表",
        "\n════════════",
    ]
    start = 0
    for m in markers:
        pos = storyboard_text.find(m)
        if pos > 0:
            start = pos + len(m)
            break

    if start == 0:
        m = re.search(r'\n\*\*镜头\d{3}\*\*', storyboard_text)
        if m:
            start = m.start()

    body = storyboard_text[start:].strip() if start > 0 else storyboard_text
    return body[:600].strip()


def _filter_storyboard_output(output: str) -> str:
    """过滤推理块和 session_id，从「## 🎬 导演核算报告」行开始提取"""
    # 以「## 🎬 导演核算报告」为提取起点（锁定，不可改）
    m = re.search(r'(?:^|\n)(##\s*🎬\s*导演核算报告[^\n]*)', output)
    if m:
        body = output[m.start():].strip()
        body = re.sub(r'\n{3,}', '\n\n', body)
        body = re.sub(r'\n---\s*\n', '\n\n', body)
        return body.strip()

    # 兜底：去推理块和 session_id
    if "┌─ Reason" in output:
        nl = output.find("\n", output.find("┌─ Reason"))
        if nl > 0:
            output = output[nl+1:].strip()
    sid_pos = output.rfind("session_id:")
    if sid_pos > len(output) - 100:
        output = output[:sid_pos].strip()
    return output


def build_project_asset_prompt(scripts: list, render_type: str = None) -> str:
    """构造项目级资产提取 prompt（合并所有剧集剧本）"""
    merged = ""
    for s in scripts:
        merged += f"\n【第{s['episode_num']}集「{s['title']}」】\n{s['script']}\n"

    render_block = ""
    if render_type and render_type in RENDER_TYPES:
        r = RENDER_TYPES[render_type]
        render_block = f"\n【渲染类型指定】\n使用「{r['name']}」({r['category']})渲染风格。指令词中的渲染标签必须匹配此类型。\n"

    return f"""请提取以下剧本的全局资产清单：人物资产、场景资产、物品资产。{render_block}
【重要规则】
- 直接输出结果，不要任何分析推理过程
- 人物只描述常规默认状态，禁止事件性/情绪性/临时状态描述
- 人物/场景/物品全局去重合并，同一实体只出现一次

{merged}
请严格按照 script-asset-designer skill 的格式输出。
"""


def _filter_asset_output(output: str) -> str:
    """过滤资产提取结果：找到第一个 ### 人物1/场景1/物品1 作为真实数据起点"""
    # 推理中会引用 ## 人物资产 但不含真实的 ### 人物1：条目
    # 真实数据以 ### 人物1：/ ### 场景1：/ ### 物品1： 开始
    import re
    for marker in [r'###\s*人物1[：:]', r'###\s*场景1[：:]', r'###\s*物品1[：:]']:
        m = re.search(marker, output)
        if m:
            # 从这个 ### 条目向前找最近的 ## 标题行，从那行开始取
            start = m.start()
            prev_section = output.rfind('\n## ', 0, start)
            if prev_section >= 0:
                start = prev_section + 1  # +1 跳过换行符
            return output[start:]
    # 降级：找最后一个 ## 人物资产
    last_idx = output.rfind('## 人物资产')
    if last_idx >= 0:
        return output[last_idx:]
    return output


def extract_asset_sections(storyboard: str) -> dict:
    """从分镜输出中提取资产相关段落（## 人物资产/场景资产/物品资产）"""
    sections = {}
    for key in ("人物资产", "场景资产", "物品资产"):
        m = re.search(rf'##\s+{key}\s*\n(.*?)(?=\n##\s|\n#+\s|\Z)', storyboard, re.DOTALL)
        if m and m.group(1).strip():
            sections[key] = m.group(1).strip()
    return sections


def merge_asset_cache(existing: str, new_sections: dict) -> str:
    """合并资产缓存：新增/更新资产条目"""
    if not existing:
        return "\n\n".join(f"## {k}\n{v}" for k, v in new_sections.items() if v)

    result = existing
    for key, content in new_sections.items():
        if not content:
            continue
        m = re.search(rf'##\s+{key}\s*\n', result)
        if m:
            pos = m.end()
            end_m = re.search(r'\n##\s', result[pos:])
            end = end_m.start() + pos if end_m else len(result)
            result = result[:pos] + content + "\n" + result[end:]
        else:
            result += f"\n\n## {key}\n{content}"
    return result.strip()


def run_agent(task_id: str, script: str, episode_id: str,
              previous_summaries: list = None, style_id: str = None,
              render_type: str = None, project_id: str = None,
              mode: str = "storyboard"):
    """后台运行 Hermes agent，根据 mode 切换分镜/资产模式"""
    try:
        with tasks_lock:
            tasks[task_id]["status"] = "processing"

        is_asset = (mode == "asset")
        if is_asset:
            prompt = build_asset_prompt(script, render_type=render_type)
            profile_name = "asset-designer"
        else:
            prompt = build_storyboard_prompt(script, previous_summaries, style_id, render_type)
            profile_name = "storyboard"

        env = os.environ.copy()
        base = os.environ.get("HERMES_PROFILES", os.path.expanduser("~/.hermes/profiles"))
        env["HERMES_HOME"] = os.path.join(base, profile_name)

        result = subprocess.run(
            [HERMES_BIN, "-p", profile_name, "chat",
             "-q", prompt, "--quiet"],
            capture_output=True, text=True, timeout=7200,
            env=env, cwd=os.path.expanduser("~"),
        )

        combined = result.stdout + "\n" + (result.stderr or "")
        output = combined.strip()
        if result.returncode != 0:
            output = result.stderr.strip() or output or "Agent 执行出错"
        elif is_asset:
            output = _filter_asset_output(output)
        else:
            output = _filter_storyboard_output(output)

        conn = get_db()
        conn.execute(
            "UPDATE episodes SET storyboard = ?, status = 'completed' WHERE id = ?",
            (output, episode_id)
        )
        conn.commit()

        if project_id:
            proj_row = conn.execute(
                "SELECT name FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            ep_row = conn.execute(
                "SELECT episode_num, title, storyboard FROM episodes WHERE id = ?",
                (episode_id,)
            ).fetchone()
            if proj_row and ep_row:
                save_to_disk(proj_row["name"], ep_row["episode_num"], ep_row["title"], ep_row["storyboard"])
                # 自动提取资产段落并合并到项目 asset_cache
                sections = extract_asset_sections(ep_row["storyboard"])
                if sections:
                    existing = conn.execute(
                        "SELECT asset_cache FROM projects WHERE id = ?", (project_id,)
                    ).fetchone()
                    merged = merge_asset_cache((existing["asset_cache"] or "") if existing else "", sections)
                    conn.execute(
                        "UPDATE projects SET asset_cache = ? WHERE id = ?",
                        (merged, project_id)
                    )
                    conn.commit()

        conn.close()

        with tasks_lock:
            tasks[task_id]["status"] = "completed"
            tasks[task_id]["result"] = output

    except subprocess.TimeoutExpired:
        with tasks_lock:
            tasks[task_id]["status"] = "timeout"
            tasks[task_id]["result"] = "生成超时（超过120分钟），请缩短剧本后重试。"
        conn = get_db()
        conn.execute("UPDATE episodes SET status = 'timeout' WHERE id = ?", (episode_id,))
        conn.commit(); conn.close()
    except Exception as e:
        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["result"] = f"处理出错: {str(e)}"
        conn = get_db()
        conn.execute("UPDATE episodes SET status = 'error' WHERE id = ?", (episode_id,))
        conn.commit(); conn.close()

    finally:
        with generating_lock:
            generating.discard(episode_id)


# ====== 前端页面 ======

@app.route("/")
def index():
    import time
    resp = app.make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    # 在 HTML 中注入版本号强制浏览器刷新
    html = resp.get_data(as_text=True)
    html = html.replace("</body>", f'<script>document.body.dataset.v="{int(time.time())}"</script></body>')
    resp.set_data(html)
    return resp


# ====== 项目 API ======

@app.route("/api/projects", methods=["GET"])
def list_projects():
    conn = get_db()
    rows = conn.execute(
        "SELECT p.*, COUNT(e.id) as episode_count FROM projects p "
        "LEFT JOIN episodes e ON p.id = e.project_id "
        "GROUP BY p.id ORDER BY p.created_at DESC"
    ).fetchall()
    conn.close()
    return jsonify([{
        "id": r["id"], "name": r["name"], "description": r["description"],
        "episode_count": r["episode_count"], "created_at": r["created_at"]
    } for r in rows])


@app.route("/api/projects", methods=["POST"])
def create_project():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "项目名称不能为空"}), 400

    project_id = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM projects WHERE name = ?", (name,)
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": f"项目名称「{name}」已存在，请换一个名称"}), 409

    conn.execute(
        "INSERT INTO projects (id, name, description, style_id, render_type, created_at) VALUES (?,?,?,?,?,?)",
        (project_id, name, data.get("description", ""),
         (data.get("style_id") or "").strip(),
         (data.get("render_type") or "").strip(),
         now)
    )
    conn.commit()
    conn.close()
    return jsonify({"id": project_id, "name": name, "created_at": now})


@app.route("/api/styles", methods=["GET"])
def get_styles():
    return jsonify({
        "styles": [
            {"id": k, "name": v["name"], "en": v["en"]}
            for k, v in STYLES.items()
        ],
        "render_types": [
            {"id": k, "name": v["name"], "category": v["category"]}
            for k, v in RENDER_TYPES.items()
        ]
    })


# ====== 设置 API ======

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_settings(data):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(load_settings())

@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.get_json()
    current = load_settings()
    current.update(data)
    save_settings(current)
    return jsonify({"ok": True})


@app.route("/api/projects/<project_id>", methods=["GET"])
def get_project(project_id):
    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not proj:
        conn.close()
        return jsonify({"error": "项目不存在"}), 404

    episodes = conn.execute(
        "SELECT * FROM episodes WHERE project_id = ? ORDER BY episode_num",
        (project_id,)
    ).fetchall()
    conn.close()

    return jsonify({
        "id": proj["id"], "name": proj["name"],
        "description": proj["description"], "created_at": proj["created_at"],
        "asset_cache": proj["asset_cache"] or "",
        "style_id": proj["style_id"] or "",
        "render_type": proj["render_type"] or "",
        "episodes": [{
            "id": e["id"], "episode_num": e["episode_num"],
            "title": e["title"], "script": e["script"],
            "storyboard": e["storyboard"] or "",
            "storyboard_has": bool(e["storyboard"]),
            "status": e["status"],
            "style_id": e["style_id"] or "",
            "render_type": e["render_type"] or "",
            "prompt": e["prompt"] or "",
            "prompt_status": e["prompt_status"] or "",
            "prompt_has": bool(e["prompt"]),
            "created_at": e["created_at"]
        } for e in episodes]
    })


@app.route("/api/projects/<project_id>", methods=["DELETE"])
def delete_project(project_id):
    conn = get_db()
    conn.execute("DELETE FROM episodes WHERE project_id = ?", (project_id,))
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/projects/<project_id>", methods=["PATCH"])
def update_project(project_id):
    data = request.get_json()
    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not proj:
        conn.close()
        return jsonify({"error": "项目不存在"}), 404

    updated = False
    if "style_id" in data:
        conn.execute("UPDATE projects SET style_id = ? WHERE id = ?",
                     (data["style_id"], project_id))
        updated = True
    if "render_type" in data:
        conn.execute("UPDATE projects SET render_type = ? WHERE id = ?",
                     (data["render_type"], project_id))
        updated = True
    if "asset_cache" in data:
        conn.execute("UPDATE projects SET asset_cache = ? WHERE id = ?",
                     (data["asset_cache"], project_id))
        updated = True
    if updated:
        conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/projects/<project_id>/extract-assets", methods=["POST"])
def extract_project_assets(project_id):
    """项目级资产提取：合并所有剧集剧本 → asset-designer agent"""
    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not proj:
        conn.close()
        return jsonify({"error": "项目不存在"}), 404

    episodes = conn.execute(
        "SELECT episode_num, title, script FROM episodes WHERE project_id = ? ORDER BY episode_num",
        (project_id,)
    ).fetchall()
    conn.close()

    if not episodes:
        return jsonify({"error": "项目没有剧集"}), 400

    scripts = [{"episode_num": e["episode_num"], "title": e["title"], "script": e["script"]}
               for e in episodes]
    render_type = proj["render_type"] or ""
    prompt = build_project_asset_prompt(scripts, render_type)

    env = os.environ.copy()
    base = os.environ.get("HERMES_PROFILES", os.path.expanduser("~/.hermes/profiles"))
    env["HERMES_HOME"] = os.path.join(base, "asset-designer")

    try:
        result = subprocess.run(
            [HERMES_BIN, "-p", "asset-designer", "chat",
             "-q", prompt, "--quiet"],
            capture_output=True, text=True, timeout=7200,
            env=env, cwd=os.path.expanduser("~"),
        )
        combined = result.stdout + "\n" + (result.stderr or "")
        output = combined.strip()
        if result.returncode != 0:
            output = result.stderr.strip() or output or "Agent 执行出错"
        else:
            output = _filter_asset_output(output)

        conn2 = get_db()
        conn2.execute("UPDATE projects SET asset_cache = ? WHERE id = ?",
                      (output, project_id))
        conn2.commit()
        conn2.close()

        return jsonify({"ok": True, "asset_cache": output})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "资产提取超时（超过120分钟）"}), 500
    except Exception as e:
        return jsonify({"error": f"资产提取失败: {str(e)}"}), 500

# ====== 格式转化 ======

def convert_to_standard_format(text):
    t = text
    t = re.sub(r'专业角色设计参考图，"([^"]+)"',
               r'professional character design sheet for "\1"', t)
    t = re.sub(r'，【身份/背景】', r',\n【身份/背景】\n', t)
    t = re.sub(r'，【外貌特征】', r'\n\n【外貌特征】\n', t)
    t = re.sub(r'，【辨识标记】', r'\n【辨识标记】\n', t)
    t = re.sub(r'，【色彩锚点】', r'\n【色彩锚点】\n', t)
    t = re.sub(r'，【皮肤纹理】', r'\n【皮肤纹理】\n', t)
    t = re.sub(r'，【发型】', r'\n【发型】\n', t)
    t = re.sub(r'，【服装】', r'\n【服装】\n', t)
    t = re.sub(r'，【人物关系】', r'\n\n【人物关系】\n', t)
    t = re.sub(r'角色参考图版式', 'character reference sheet layout', t)
    t = re.sub(r'pure solid white background, isolated character on white background, absolutely no background scenery',
               'white background, clean presentation', t)
    t = re.sub(r'场景概念设计图，"([^"]+)"',
               r'scene concept design for "\1"', t)
    t = re.sub(r'物品概念设计图，"([^"]+)"',
               r'prop concept design for "\1"', t)
    if 'detailed background' not in t:
        for tag in ['detailed illustration, concept art, character model sheet',
                    'detailed illustration, concept art, environment design',
                    'detailed illustration, concept art, prop design']:
            if tag in t:
                t = t.replace(tag, 'detailed background, ' + tag)
                break
    # 去掉 "- 中文指令词：" 前缀
    t = re.sub(r'- 中文指令词：', '', t)
    t = re.sub(r'- 负向提示词：\n[^\n]*\n(?:  -[^\n]*\n)*', '', t)
    return t


@app.route("/api/projects/<project_id>/convert-assets", methods=["POST"])
def convert_assets(project_id):
    conn = get_db()
    proj = conn.execute("SELECT asset_cache FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not proj:
        conn.close()
        return jsonify({"error": "项目不存在"}), 404
    original = proj["asset_cache"] or ""
    if not original:
        conn.close()
        return jsonify({"error": "请先提取项目资产"}), 400
    converted = convert_to_standard_format(original)
    conn.execute("UPDATE projects SET asset_cache = ? WHERE id = ?", (converted, project_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "asset_cache": converted})


# ====== 剧集 API ======

@app.route("/api/projects/<project_id>/episodes", methods=["POST"])
def add_episode(project_id):
    data = request.get_json()
    script = (data.get("script") or "").strip()
    if not script:
        return jsonify({"error": "剧本内容不能为空"}), 400

    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not proj:
        conn.close()
        return jsonify({"error": "项目不存在"}), 404

    max_ep = conn.execute(
        "SELECT COALESCE(MAX(episode_num), 0) as m FROM episodes WHERE project_id = ?",
        (project_id,)
    ).fetchone()["m"]
    episode_num = max_ep + 1

    title = (data.get("title") or f"第{episode_num}集").strip()
    # 渲染类型：优先使用请求中的，否则继承项目默认
    render_type_req = (data.get("render_type") or "").strip()
    episode_render_type = render_type_req if render_type_req else (proj["render_type"] or "")
    episode_id = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()

    conn.execute(
        "INSERT INTO episodes (id, project_id, episode_num, title, script, render_type, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (episode_id, project_id, episode_num, title, script, episode_render_type, "pending", now)
    )
    conn.commit()

    trigger = data.get("generate", True)
    if not isinstance(trigger, bool):
        trigger = True

    task_id = None
    previous_summaries = []
    if trigger:
        prev_episodes = conn.execute(
            "SELECT episode_num, title, storyboard FROM episodes "
            "WHERE project_id = ? AND episode_num < ? AND storyboard != '' "
            "ORDER BY episode_num",
            (project_id, episode_num)
        ).fetchall()

        for pe in prev_episodes:
            previous_summaries.append({
                "episode_num": pe["episode_num"],
                "title": pe["title"],
                "summary": summarize_storyboard(pe["storyboard"])
            })

        style_id = proj["style_id"] or ""
        conn.close()

        task_id = str(uuid.uuid4())[:8]
        with tasks_lock:
            tasks[task_id] = {
                "status": "queued",
                "script_preview": script[:100] + ("..." if len(script) > 100 else ""),
                "result": None,
                "episode_id": episode_id,
                "project_id": project_id,
                "episode_num": episode_num,
            }

        thread = threading.Thread(
            target=run_agent,
            args=(task_id, script, episode_id,
                  previous_summaries if previous_summaries else None,
                  style_id, episode_render_type, project_id, "storyboard")
        )
        thread.daemon = True
        thread.start()
    else:
        conn.close()

    return jsonify({
        "task_id": task_id, "episode_id": episode_id,
        "episode_num": episode_num,
        "status": "queued" if trigger else "confirmed",
        "has_previous": len(previous_summaries) > 0 if trigger else False,
    })


@app.route("/api/projects/<project_id>/episodes/<episode_id>/generate", methods=["POST"])
def generate_episode(project_id, episode_id):
    conn = get_db()
    ep = conn.execute(
        "SELECT * FROM episodes WHERE id = ? AND project_id = ?",
        (episode_id, project_id)
    ).fetchone()
    if not ep:
        conn.close()
        return jsonify({"error": "剧集不存在"}), 404

    data = request.get_json() or {}
    request_mode = (data.get("mode") or ep["mode"] or "storyboard").strip()

    with generating_lock:
        if episode_id in generating:
            conn.close()
            return jsonify({"error": "该剧集正在生成中，请等待完成"}), 409
        generating.add(episode_id)

    conn.execute(
        "UPDATE episodes SET status = 'pending', storyboard = NULL, mode = ? WHERE id = ?",
        (request_mode, episode_id)
    )
    conn.commit()

    prev_episodes = conn.execute(
        "SELECT episode_num, title, storyboard FROM episodes "
        "WHERE project_id = ? AND episode_num < ? AND storyboard != '' "
        "ORDER BY episode_num",
        (project_id, ep["episode_num"])
    ).fetchall()
    conn.close()

    previous_summaries = []
    for pe in prev_episodes:
        previous_summaries.append({
            "episode_num": pe["episode_num"],
            "title": pe["title"],
            "summary": summarize_storyboard(pe["storyboard"])
        })

    conn2 = get_db()
    proj = conn2.execute(
        "SELECT style_id, render_type FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    style_id = proj["style_id"] or "" if proj else ""
    # 优先使用剧集自身渲染类型，否则用项目默认
    render_type = ep["render_type"] or (proj["render_type"] or "") if proj else (ep["render_type"] or "")
    conn2.close()

    task_id = str(uuid.uuid4())[:8]
    with tasks_lock:
        tasks[task_id] = {
            "status": "queued",
            "script_preview": ep["script"][:100] + ("..." if len(ep["script"]) > 100 else ""),
            "result": None,
            "episode_id": episode_id,
            "project_id": project_id,
            "episode_num": ep["episode_num"],
        }

    thread = threading.Thread(
        target=run_agent,
        args=(task_id, ep["script"], episode_id,
              previous_summaries if previous_summaries else None,
              style_id, render_type, project_id, request_mode)
    )
    thread.daemon = True
    thread.start()

    return jsonify({
        "task_id": task_id, "episode_id": episode_id,
        "episode_num": ep["episode_num"], "status": "queued",
        "has_previous": len(previous_summaries) > 0,
    })


@app.route("/api/projects/<project_id>/episodes/<episode_id>", methods=["DELETE"])
def delete_episode(project_id, episode_id):
    conn = get_db()
    ep = conn.execute(
        """SELECT e.*, p.name as project_name
           FROM episodes e JOIN projects p ON e.project_id = p.id
           WHERE e.id = ? AND e.project_id = ?""",
        (episode_id, project_id)
    ).fetchone()
    if not ep:
        conn.close()
        return jsonify({"error": "剧集不存在"}), 404

    conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
    conn.commit()
    conn.close()

    delete_from_disk(ep["project_name"], ep["episode_num"], ep["title"])

    return jsonify({"ok": True, "deleted": {"episode_num": ep["episode_num"], "title": ep["title"]}})


@app.route("/api/projects/<project_id>/episodes/<episode_id>", methods=["PATCH"])
def update_episode(project_id, episode_id):
    data = request.get_json()
    conn = get_db()
    ep = conn.execute(
        "SELECT * FROM episodes WHERE id = ? AND project_id = ?",
        (episode_id, project_id)
    ).fetchone()
    if not ep:
        conn.close()
        return jsonify({"error": "剧集不存在"}), 404

    updated = False
    if "style_id" in data:
        conn.execute("UPDATE episodes SET style_id = ? WHERE id = ?",
                     (data["style_id"], episode_id))
        updated = True
    if "render_type" in data:
        conn.execute("UPDATE episodes SET render_type = ? WHERE id = ?",
                     (data["render_type"], episode_id))
        updated = True
    if updated:
        conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/projects/<project_id>/episodes/<episode_id>/clear-storyboard", methods=["POST"])
def clear_episode_storyboard(project_id, episode_id):
    conn = get_db()
    ep = conn.execute(
        """SELECT e.*, p.name as project_name
           FROM episodes e JOIN projects p ON e.project_id = p.id
           WHERE e.id = ? AND e.project_id = ?""",
        (episode_id, project_id)
    ).fetchone()
    if not ep:
        conn.close()
        return jsonify({"error": "剧集不存在"}), 404

    conn.execute("UPDATE episodes SET storyboard = NULL WHERE id = ?", (episode_id,))
    conn.commit()
    conn.close()

    delete_from_disk(ep["project_name"], ep["episode_num"], ep["title"])
    return jsonify({"ok": True, "cleared": "storyboard"})


@app.route("/api/episodes/<episode_id>/prompt", methods=["DELETE"])
def clear_episode_prompt(episode_id):
    conn = get_db()
    conn.execute("UPDATE episodes SET prompt = '', prompt_status = '' WHERE id = ?", (episode_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "cleared": "prompt"})

    return jsonify({"ok": True})


@app.route("/api/episodes/<episode_id>/video-segments", methods=["GET"])
def get_video_segments(episode_id):
    conn = get_db()
    ep = conn.execute("SELECT video_segments FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    conn.close()
    if not ep or not ep["video_segments"]:
        return jsonify({"segments": []})
    try:
        data = json.loads(ep["video_segments"])
        return jsonify(data)
    except:
        return jsonify({"segments": []})


@app.route("/api/episodes/<episode_id>/video-segments", methods=["POST"])
def save_video_segments(episode_id):
    data = request.get_json()
    conn = get_db()
    conn.execute("UPDATE episodes SET video_segments = ? WHERE id = ?",
                 (json.dumps(data, ensure_ascii=False), episode_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/projects/<project_id>/export-prompts", methods=["GET"])
def export_prompts_zip(project_id):
    import io, zipfile
    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not proj:
        conn.close()
        return jsonify({"error": "项目不存在"}), 404

    episodes = conn.execute(
        "SELECT * FROM episodes WHERE project_id = ? ORDER BY episode_num",
        (project_id,)
    ).fetchall()
    conn.close()

    completed = [e for e in episodes if e["prompt"] and e["prompt_status"] == "completed"]
    if not completed:
        return jsonify({"error": "没有已完成的提示词"}), 400

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for ep in completed:
            filename = f"{proj['name']}_第{ep['episode_num']}集_提示词.txt"
            text = f"项目：{proj['name']}\n第{ep['episode_num']}集：{ep['title']}\n\n{ep['prompt']}"
            zf.writestr(filename, text)

    buf.seek(0)
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True,
                     download_name=f"{proj['name']}_提示词合集.zip")


@app.route("/api/tasks/<task_id>", methods=["GET"])
def get_task(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(task)


# ====== 视频提示词生成 ======

def extract_asset_names(asset_cache: str) -> str:
    """从资产缓存中提取资产名称清单（仅名字列表，不含设计内容）"""
    if not asset_cache:
        return ""

    sections = []
    for section_key, prefix in [("人物资产", "人物"), ("场景资产", "场景"), ("物品资产", "物品")]:
        idx = asset_cache.find(f"## {section_key}")
        if idx < 0:
            continue
        section = asset_cache[idx + len(f"## {section_key}"):]
        next_idx = re.search(r'\n##\s', section)
        if next_idx:
            section = section[:next_idx.start()]

        names = re.findall(rf'###\s*{prefix}\d+[：:]\s*(.+)', section)
        if names:
            names_clean = [n.strip() for n in names if n.strip() and len(n.strip()) < 60]
            if names_clean:
                sections.append(f"{section_key}：{'、'.join(names_clean)}")

    return "\n".join(sections) if sections else ""


def _filter_seedance_output(output: str) -> str:
    """过滤 seedance agent 输出：以 ## 🎬 导演核算报告 为提取起点"""
    # 新提取点：## 🎬 导演核算报告
    marker = "## 🎬 导演核算报告"
    idx = output.find(marker)
    if idx >= 0:
        return output[idx:]
    # 降级：🎬 导演核算报告（无 ##）
    idx = output.find("🎬 导演核算报告")
    if idx >= 0:
        return output[idx:]
    return output


def run_seedance_agent(task_id: str, storyboard: str, asset_cache: str,
                       episode_id: str, episode_title: str,
                       style_id: str = None, render_type: str = None):
    """后台运行 seedance-prompt agent"""
    try:
        with tasks_lock:
            tasks[task_id]["status"] = "processing"

        style_info = ""
        if style_id and style_id in STYLES:
            s = STYLES[style_id]
            style_info = f"\n指定视觉风格：「{s['name']}」({s['en']})"
        if render_type and render_type in RENDER_TYPES:
            r = RENDER_TYPES[render_type]
            style_info += f"\n指定渲染类型：「{r['name']}」({r['category']})"

        asset_block = ""
        if asset_cache:
            asset_names = extract_asset_names(asset_cache)
            asset_block = f"\n\n【项目资产】\n{asset_names}\n"

        prompt = f"""【重要：全部使用中文输出，直接在回复正文中输出完整提示词，不要写入文件】

为以下分镜脚本生成 Seedance 2.0 视频生成提示词。{style_info}

剧集：{episode_title}
{asset_block}
【分镜脚本 — 完整版】
{storyboard}

请严格按照 seedance-prompt-generator skill 的 Director Angel 编译格式输出，包括：
1. 导演核算报告（总时长+拆段）
2. 每个 Segment 含 Asset Definitions / Global Style / Base Compiled Prompt / Director's Shot Matrix / Native Audio
3. 全中文输出，运镜术语保留英文"""

        env = os.environ.copy()
        base = os.environ.get("HERMES_PROFILES", os.path.expanduser("~/.hermes/profiles"))
        env["HERMES_HOME"] = os.path.join(base, "seedance-prompt")

        result = subprocess.run(
            [HERMES_BIN, "-p", "seedance-prompt", "chat",
             "-q", prompt, "--quiet", "-Q"],
            capture_output=True, text=True, timeout=7200,
            env=env, cwd=os.path.expanduser("~"),
        )

        combined = result.stdout + "\n" + (result.stderr or "")
        output = combined.strip()
        if result.returncode != 0:
            output = result.stderr.strip() or output or "Agent 执行出错"
        else:
            output = _filter_seedance_output(output)

        conn = get_db()
        conn.execute(
            "UPDATE episodes SET prompt = ?, prompt_status = 'completed' WHERE id = ?",
            (output, episode_id)
        )
        conn.commit()
        conn.close()

        with tasks_lock:
            tasks[task_id]["status"] = "completed"
            tasks[task_id]["result"] = output

    except subprocess.TimeoutExpired:
        with tasks_lock:
            tasks[task_id]["status"] = "timeout"
            tasks[task_id]["result"] = "提示词生成超时（超过120分钟）"
        conn = get_db()
        conn.execute("UPDATE episodes SET prompt_status = 'timeout' WHERE id = ?",
                     (episode_id,))
        conn.commit(); conn.close()
    except Exception as e:
        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["result"] = f"处理出错: {str(e)}"
        conn = get_db()
        conn.execute("UPDATE episodes SET prompt_status = 'error' WHERE id = ?",
                     (episode_id,))
        conn.commit(); conn.close()


@app.route("/api/projects/<project_id>/episodes/<episode_id>/generate-prompt",
           methods=["POST"])
def generate_prompt(project_id, episode_id):
    conn = get_db()
    ep = conn.execute(
        "SELECT * FROM episodes WHERE id = ? AND project_id = ?",
        (episode_id, project_id)
    ).fetchone()
    if not ep:
        conn.close()
        return jsonify({"error": "剧集不存在"}), 404

    proj = conn.execute(
        "SELECT asset_cache, style_id, render_type FROM projects WHERE id = ?",
        (project_id,)
    ).fetchone()
    conn.close()

    storyboard = (ep["storyboard"] or "").strip()
    if not storyboard:
        return jsonify({"error": "该剧集还没有分镜，先生成分镜"}), 400

    asset_cache = (proj["asset_cache"] or "") if proj else ""
    task_id = str(uuid.uuid4())[:8]

    with tasks_lock:
        tasks[task_id] = {
            "status": "queued",
            "script_preview": f"视频提示词: 第{ep['episode_num']}集 {ep['title']}",
            "result": None,
            "episode_id": episode_id,
            "project_id": project_id,
        }

    thread = threading.Thread(
        target=run_seedance_agent,
        args=(task_id, storyboard, asset_cache, episode_id, ep["title"],
              (proj["style_id"] or "") if proj else "",
              (proj["render_type"] or "") if proj else "")
    )
    thread.daemon = True
    thread.start()

    return jsonify({
        "task_id": task_id, "episode_id": episode_id,
        "status": "queued",
    })


# ====== 兼容旧接口 ======

@app.route("/api/storyboard", methods=["POST"])
def submit_script_legacy():
    data = request.get_json()
    script = (data.get("script") or "").strip()
    if not script:
        return jsonify({"error": "剧本内容不能为空"}), 400

    conn = get_db()
    proj = conn.execute(
        "SELECT id FROM projects WHERE name = '默认项目' LIMIT 1"
    ).fetchone()
    if not proj:
        proj_id = str(uuid.uuid4())[:8]
        conn.execute(
            "INSERT INTO projects (id, name, created_at) VALUES (?,?,?)",
            (proj_id, "默认项目", datetime.now().isoformat())
        )
        conn.commit()
    else:
        proj_id = proj["id"]
    conn.close()

    return add_episode(proj_id)


def _validate_js() -> bool:
    import tempfile, shutil
    html_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    if not os.path.exists(html_path):
        return True
    node_bin = shutil.which("node")
    if not node_bin:
        try: print("[WARN] node.js not installed, skipping JS check")
        except: pass
        return True
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    errors = []
    for i, script in enumerate(scripts):
        if not script.strip():
            continue
        fd, tmp = tempfile.mkstemp(suffix=".js")
        try:
            os.write(fd, script.encode("utf-8"))
            os.close(fd)
            r = subprocess.run([node_bin, "--check", tmp],
                               capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                errors.append(f"  script 块 #{i+1}:\n{r.stderr.strip()}")
        finally:
            os.unlink(tmp)
    if errors:
        try:
            print("\n" + "=" * 60)
            print("[ERROR] index.html JS syntax error, server refused!")
            print("=" * 60)
            for e in errors:
                print(e)
            print("=" * 60)
            print("Fix index.html and restart.\n")
        except: pass
        return False
    try:
        print(f"[OK] JS check passed ({len([s for s in scripts if s.strip()])} script blocks)")
    except: pass
    return True




# ====== 生图代理 ======

import base64 as _base64
base64 = _base64

@app.route("/api/list-models", methods=["POST"])
def list_models_proxy():
    data = request.get_json()
    api_url = (data.get("apiUrl") or "").strip()
    api_key = (data.get("apiKey") or "").strip()
    if not api_url:
        return jsonify({"models": []})
    base = api_url.rstrip("/").replace("/v1/images/generations", "").replace("/v1/chat/completions", "").replace("/v1", "")
    models_url = base + "/v1/models"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = requests.get(models_url, headers=headers, timeout=15)
        if resp.status_code == 200:
            result = resp.json()
            models = [m["id"] for m in result.get("data", []) if "id" in m]
            return jsonify({"models": sorted(models)})
    except:
        pass
    return jsonify({"models": []})


@app.route("/api/images/<path:filepath>")
def serve_image(filepath):
    full_path = os.path.join(OUTPUT_DIR, filepath)
    if not os.path.exists(full_path):
        return jsonify({"error": "not found"}), 404
    return send_file(full_path, mimetype="image/png")


@app.route("/api/open-folder", methods=["POST"])
def open_folder():
    data = request.get_json()
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "no path"}), 400
    folder = os.path.dirname(path)
    if not os.path.exists(folder):
        return jsonify({"error": f"folder not found: {folder}"}), 404
    # 转换 WSL 路径到 Windows 路径
    win_path = folder.replace("/mnt/c/", "C:\\").replace("/mnt/d/", "D:\\")
    win_path = win_path.replace("/", "\\")
    # 直接用 explorer.exe（WSL 中可以调用 Windows 程序）
    try:
        subprocess.Popen(["explorer.exe", win_path])
    except:
        try:
            subprocess.Popen(["cmd.exe", "/c", "start", "", win_path])
        except Exception as e:
            return jsonify({"error": f"无法打开: {str(e)}", "path": win_path}), 500
    return jsonify({"ok": True, "opened": win_path})


# Simple in-memory cache for asset images
_asset_cache = {}

@app.route("/api/projects/<project_id>/asset-images")
def get_asset_images(project_id):
    now = time.time()
    if project_id in _asset_cache and now - _asset_cache[project_id]["ts"] < 10:
        return jsonify(_asset_cache[project_id]["data"])
    
    conn = get_db()
    proj = conn.execute("SELECT name FROM projects WHERE id = ?", (project_id,)).fetchone()
    conn.close()
    if not proj:
        return jsonify({"error": "project not found"}), 404
    assets_dir = os.path.join(OUTPUT_DIR, proj["name"], "assets")
    result = {}
    if os.path.isdir(assets_dir):
        for asset_name in sorted(os.listdir(assets_dir)):
            asset_path = os.path.join(assets_dir, asset_name)
            if os.path.isdir(asset_path):
                imgs = [f for f in os.listdir(asset_path) if f.lower().endswith(('.png','.jpg','.jpeg'))]
                if imgs:
                    imgs.sort(reverse=True)
                    # Check if user selected a specific image
                    sel_key = f"selected_{proj['name']}_{asset_name}"
                    # Can't read localStorage from server, use query param or header
                    # Instead, always return all info, let frontend pick
                    latest = imgs[0]
                    result[asset_name] = {
                        "latest": f"/api/images/{proj['name']}/assets/{asset_name}/{latest}",
                        "selected": None,  # frontend will check localStorage
                        "count": len(imgs),
                        "files": imgs[:10]
                    }
    _asset_cache[project_id] = {"ts": time.time(), "data": result}
    # Add audio selections
    for asset_name in result:
        ak = f"selected_audio_{proj['name']}_{asset_name}"
        if ak in _audio_selections:
            result[asset_name]["selected_audio"] = _audio_selections[ak]
    _asset_cache[project_id] = {"ts": time.time(), "data": result}
    return jsonify(result)


@app.route("/api/upload-asset", methods=["POST"])
def upload_asset():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    file = request.files["file"]
    project = (request.form.get("project") or "default").strip()
    asset = (request.form.get("asset") or "untitled").strip()
    safe_asset = re.sub(r'[\\/*?:"<>|]', '_', asset)
    assets_dir = os.path.join(OUTPUT_DIR, project, "assets", safe_asset)
    os.makedirs(assets_dir, exist_ok=True)
    filename = file.filename or "upload"
    safe_name = re.sub(r'[\\/*?:"<>|]', '_', filename)
    from datetime import datetime as _dt
    ts = _dt.now().strftime('%Y%m%d_%H%M%S')
    final_name = f"{safe_asset}_{ts}_{safe_name}"
    filepath = os.path.join(assets_dir, final_name)
    file.save(filepath)
    filetype = "audio" if file.mimetype and file.mimetype.startswith("audio/") else "image"
    return jsonify({"ok": True, "filename": final_name, "type": filetype,
                    "url": f"/api/images/{project}/assets/{safe_asset}/{final_name}"})


@app.route("/api/clear-audio", methods=["POST"])
def clear_audio():
    data = request.get_json()
    proj = data.get("project", "")
    asset = data.get("asset", "")
    conn = get_db()
    conn.execute("DELETE FROM audio_selections WHERE project=? AND asset=?", (proj, asset))
    conn.commit()
    conn.close()
    _audio_selections.pop(f"selected_audio_{proj}_{asset}", None)
    _asset_cache.clear()
    return jsonify({"ok": True})


@app.route("/api/select-audio", methods=["POST"])
def select_audio():
    data = request.get_json()
    proj = data.get("project", "")
    asset = data.get("asset", "")
    filename = data.get("filename", "")
    key = f"selected_audio_{proj}_{asset}"
    # 同时写入数据库和内存
    conn = get_db()
    conn.execute("DELETE FROM audio_selections WHERE project=? AND asset=?", (proj, asset))
    conn.execute("INSERT INTO audio_selections (id, project, asset, audio_file) VALUES (?, ?, ?, ?)",
                 (uuid.uuid4().hex, proj, asset, filename))
    conn.commit()
    conn.close()
    _audio_selections[key] = filename
    return jsonify({"ok": True})


_audio_selections = {}

@app.route("/browse/<path:subpath>")
def browse_folder(subpath):
    folder = os.path.join(OUTPUT_DIR, subpath)
    if not os.path.isdir(folder):
        return "<h2>文件夹不存在</h2>", 404
    files = sorted(os.listdir(folder), reverse=True)
    all_files = [f for f in files if f.lower().endswith(('.png','.jpg','.jpeg','.gif','.webp','.mp3','.wav','.ogg','.m4a','.aac'))]
    proj_name = subpath.split('/')[0] if '/' in subpath else subpath
    asset_name = subpath.rsplit('/', 1)[-1] if '/' in subpath else subpath
    cards = []
    for f in all_files:
        url = "/api/images/%s/%s" % (subpath, f)
        is_audio = f.lower().endswith(('.mp3','.wav','.ogg','.m4a','.aac'))
        btn_label = "🎵 选为音频" if is_audio else "⭐ 选择此图"
        display = '<div class="card"><div class="name">%s</div>' % f
        if not is_audio:
            display += '<a href="%s" target="_blank"><img src="%s" loading="lazy" style="width:100%%;height:150px;object-fit:cover"></a>' % (url, url)
        else:
            display += '<div style="width:100%%;height:150px;display:flex;align-items:center;justify-content:center;font-size:40px">🎵</div>'
        display += '<button onclick="selectAssetFile(\'%s\',\'%s\',\'%s\',%s)" style="width:100%%;padding:4px;background:#238636;color:#fff;border:none;cursor:pointer;font-size:11px">%s</button></div>' % (proj_name, asset_name, f, str(is_audio).lower(), btn_label)
        cards.append(display)
    script = """<script>
var g_proj="%s",g_asset="%s";
function selectAssetFile(p,a,f,isAudio){
  var key = isAudio ? "selected_audio_"+p+"_"+a : "selected_"+p+"_"+a;
  localStorage.setItem(key, f);
  fetch("/api/select-audio",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({project:p,asset:a,filename:f})});
  document.body.style.background="#0a1a0a";
  if(window.opener&&!window.opener.closed){window.opener.postMessage({type:"asset-selected",proj:p,asset:a,file:f,isAudio:isAudio},"*");}
  setTimeout(function(){location.reload();},300);
}
window.onload=function(){
  var imgSel=localStorage.getItem("selected_"+g_proj+"_"+g_asset);
  var audSel=localStorage.getItem("selected_audio_"+g_proj+"_"+g_asset);
  var cs=document.querySelectorAll(".card");
  for(var i=0;i<cs.length;i++){
    var name=cs[i].querySelector(".name").textContent.trim();
    var btn=cs[i].querySelector("button");
    if(!btn)continue;
    if(name===imgSel){btn.style.background="#f59e0b";btn.textContent="✅ 当前图片";}
    else if(name===audSel){btn.style.background="#8b5cf6";btn.textContent="🎵 当前音频";}
  }
};
</script>""" % (proj_name, asset_name)
    resp = app.make_response("""<html><head><meta charset="utf-8"><title>资产文件夹</title>
%s
<style>
body{background:#0f1117;color:#e0e0e0;font-family:sans-serif;padding:20px}
h2{color:#58a6ff}.grid{display:flex;flex-wrap:wrap;gap:12px}
.card{width:200px;background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}
.card img{width:100%%;height:150px;object-fit:cover}
.card .name{padding:8px;font-size:11px;word-break:break-all;color:#8b949e}
.card a{color:#58a6ff;text-decoration:none}
</style></head><body>
<h2>📁 %s</h2><p>%d 个文件</p><div class="grid">%s</div></body></html>""" % (script, subpath, len(all_files), ''.join(cards)))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp

@app.route("/api/generate-image", methods=["POST"])
def generate_image_proxy():
    try:
        data = request.get_json()
        api_url = (data.get("apiUrl") or "").strip()
        api_key = (data.get("apiKey") or "").strip()
        prompt = (data.get("prompt") or "").strip()
        negative = (data.get("negativePrompt") or "").strip()
        model = (data.get("model") or "gpt-image-2-reverse").strip()
        resolution = (data.get("resolution") or "2K").strip()
        ratio = (data.get("ratio") or "1:1").strip()
        ref_images = data.get("referenceImages") or []

        if not api_url or not prompt:
            return jsonify({"error": "缺少API地址或提示词"}), 400

        # 解析宽高比 → OpenAI size 格式
        ratio_parts = ratio.split(":")
        if len(ratio_parts) == 2:
            w, h = int(ratio_parts[0]), int(ratio_parts[1])
            base = {"1K": 1024, "2K": 2048, "4K": 4096}.get(resolution, 2048)
            scale = base / max(w, h)
            size = f"{int(w * scale)}x{int(h * scale)}"
        else:
            size = "1024x1024"

        full_prompt = prompt
        base_url = api_url.rstrip("/").replace("/v1/images/generations", "").replace("/v1/chat/completions", "").replace("/v1", "")
        api_url = base_url + "/v1/images/generations"

        # 转换中文参考图引用 → API 格式
        import re as _re
        full_prompt = _re.sub(r'图(\d+)', r'[image\1]', full_prompt)
        full_prompt = _re.sub(r'参考图\s*(\d+)', r'[image\1]', full_prompt)

        body = {
            "model": model,
            "prompt": full_prompt,
            "n": 1,
            "size": size,
            "quality": "high",
        }
        if ref_images and len(ref_images) > 0:
            body["image"] = ref_images[:16]
            logger.info(f"[生图] 参考图模式 refs={len(ref_images)}")

        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        resp = requests.post(api_url, json=body, headers=headers, timeout=600)

        content_type = resp.headers.get("Content-Type", "")
        logger.info(f"[生图] HTTP {resp.status_code} content-type={content_type} size={len(resp.content)}")
        if resp.status_code != 200:
            return jsonify({"error": f"API返回 {resp.status_code}: {resp.text[:500]}"}), 502

        try:
            # 二进制图片：直接保存
            if "image/" in content_type or (len(resp.content) > 500 and resp.content[:4] in (b'\x89PNG', b'\xff\xd8', b'RIFF')):
                project_name = (data.get("projectName") or "default").strip()
                asset_name = (data.get("assetName") or "untitled").strip()
                safe_name = re.sub(r'[\\/*?:"<>|]', '_', asset_name)
                from datetime import datetime as _dt
                safe_name = f"{safe_name}_{_dt.now().strftime('%Y%m%d_%H%M%S')}"
                assets_dir = os.path.join(OUTPUT_DIR, project_name, "assets", safe_name)
                os.makedirs(assets_dir, exist_ok=True)
                ext = ".png"
                if resp.content[:4] == b'\xff\xd8':
                    ext = ".jpg"
                local_path = os.path.join(assets_dir, safe_filename + ext)
                with open(local_path, 'wb') as f:
                    f.write(resp.content)
                logger.info(f"[生图] 二进制保存: {local_path} ({len(resp.content)} bytes)")
                return jsonify({"ok": True, "image_url": f"/api/images/{project_name}/assets/{safe_name}/{safe_filename}{ext}", "local_path": local_path})

            # JSON 响应 或 文本 base64
            project_name = (data.get("projectName") or "default").strip()
            asset_name = (data.get("assetName") or "untitled").strip()
            safe_name = re.sub(r'[\\/*?:"<>|]', '_', asset_name)
            from datetime import datetime as _dt
            safe_filename = f"{safe_name}_{_dt.now().strftime('%Y%m%d_%H%M%S')}"
            assets_dir = os.path.join(OUTPUT_DIR, project_name, "assets", safe_name)
            os.makedirs(assets_dir, exist_ok=True)
            ext = ".png"

            # 尝试 JSON 解析
            try:
                result = resp.json()
            except:
                result = None

            image_data = None
            image_url = None

            if isinstance(result, dict):
                data_list = result.get("data", [])
                if isinstance(data_list, list) and data_list:
                    b64 = data_list[0].get("b64_json", "")
                    url = data_list[0].get("url", "")
                    if b64:
                        try:
                            image_data = base64.b64decode(b64)
                            logger.info(f"[生图] b64_json解码成功, size={len(image_data)}")
                        except Exception as e:
                            logger.info(f"[生图] b64_json解码失败: {e}")
                    elif url:
                        # 处理 data URI 格式: data:image/png;base64,xxxx
                        if url.startswith("data:"):
                            b64_part = url.split(",", 1)[-1] if "," in url else url
                            try:
                                image_data = base64.b64decode(b64_part)
                                logger.info(f"[生图] data URI解码成功, size={len(image_data)}")
                            except Exception as e:
                                logger.info(f"[生图] data URI解码失败: {e}, b64_part前100字: {b64_part[:100]}")
                        else:
                            image_url = url
                # 也检查顶层的 b64_json
                b64 = result.get("b64_json", "") or result.get("image", "")
                if b64 and not image_data:
                    image_data = base64.b64decode(b64)
                if not image_url:
                    image_url = result.get("url") or result.get("image_url") or ""

            # 如果响应体本身就是 base64 字符串
            if not image_data and not image_url:
                text = resp.text.strip()
                if len(text) > 100 and not text.startswith("{") and not text.startswith("<"):
                    try:
                        image_data = base64.b64decode(text)
                    except:
                        pass

            # 保存图片
            local_path = ""
            if image_data and len(image_data) > 100:
                local_path = os.path.join(assets_dir, safe_filename + ext)
                with open(local_path, 'wb') as f:
                    f.write(image_data)
                logger.info(f"[生图] base64保存: {local_path} ({len(image_data)} bytes)")
            elif image_url:
                try:
                    if image_url.startswith("http"):
                        img_resp = requests.get(image_url, timeout=120)
                        if img_resp.status_code == 200 and len(img_resp.content) > 100:
                            local_path = os.path.join(assets_dir, safe_filename + ext)
                            with open(local_path, 'wb') as f:
                                f.write(img_resp.content)
                            logger.info(f"[生图] URL保存: {local_path} ({len(img_resp.content)} bytes)")
                except Exception as save_err:
                    logger.info(f"[生图] 保存失败: {save_err}")

            if not local_path and not image_url:
                logger.info(f"[生图] 未能提取图片! result keys: {list(result.keys()) if isinstance(result, dict) else 'not dict'}")
            return jsonify({"ok": True, "image_url": f"/api/images/{project_name}/assets/{safe_name}/{safe_filename}{ext}" if local_path else (image_url or ""), "local_path": local_path, "debug": {"has_data": bool(image_data), "has_url": bool(image_url), "has_path": bool(local_path)}})
        except Exception as e:
            return jsonify({"error": f"处理失败: {str(e)}"}), 500

    except Exception as e:
        return jsonify({"error": f"请求失败: {str(e)}"}), 500




@app.route("/api/video/generate/web", methods=["POST"])
def generate_web_video():
    import subprocess
    import os
    from threading import Thread

    data = request.get_json()
    episode_id = data.get("episode_id")
    segment_ids = data.get("segment_ids", [])
    if not episode_id or not segment_ids:
        return jsonify({"error": "缺少参数"}), 400

    # 异步运行生成任务
    def run_task():
        # 从数据库读取Segments
        conn = get_db()
        ep = conn.execute("SELECT video_segments FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        conn.close()
        if not ep or not ep["video_segments"]:
            return
        try:
            seg_data = json.loads(ep["video_segments"])
            segments = [s for s in seg_data["segments"] if s["id"] in segment_ids]
        except:
            return

        # 从API设置读取视频生成网址
        settings = load_settings()
        video_url = settings.get("videoApiUrl", "")
        if not video_url:
            return

        # agent-browser 执行命令（示例，后续按实际页面调整）
        cmds = [
            ["agent-browser", "open", video_url],
            ["agent-browser", "wait", "3000"],
            # 后续添加填充参数、点击生成的命令
        ]
        for cmd in cmds:
            try:
                subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except:
                pass

    Thread(target=run_task, daemon=True).start()
    return jsonify({"ok": True})


# ====== Dreamina CLI 视频生成 ======

# 内存中的视频生成任务状态
dreamina_tasks = {}
dreamina_tasks_lock = threading.Lock()

# 全局单并发锁：同一时间只允许一个视频生成任务运行
dreamina_video_queue_lock = threading.Lock()
dreamina_video_running = False
dreamina_video_queue = []  # 等待中的任务列表

def _find_dreamina_bin():
    """查找 dreamina CLI 二进制文件（Windows/Linux）"""
    import shutil as _shutil
    candidates = []
    if os.name == "nt":
        candidates = [
            os.path.expanduser("~/dreamina.exe"),  # C:\Users\Administrator\dreamina.exe（无中文）
            os.path.join(os.path.dirname(__file__), "dreamina.exe"),  # 项目目录
            os.path.expanduser("~/.local/bin/dreamina.exe"),
        ]
    else:
        candidates = [
            os.path.join(os.path.dirname(__file__), "dreamina"),
            os.path.expanduser("~/.local/bin/dreamina"),
        ]
    for c in candidates:
        if os.path.exists(c):
            return c
    # 尝试从 PATH 查找
    which = _shutil.which("dreamina.exe" if os.name == "nt" else "dreamina")
    if which:
        return which
    return None

DREAMINA_BIN = _find_dreamina_bin()

def _dreamina_cmd():
    """返回运行 dreamina 的命令前缀列表（仅 Windows 原生）"""
    if DREAMINA_BIN:
        return [DREAMINA_BIN]
    return ["dreamina"]  # 最后的兜底：尝试 PATH


def _to_wsl_path(win_path):
    """Windows 原生模式：路径不再需要 WSL 转换，直接返回"""
    return win_path


def _dreamina_available():
    """检查 dreamina CLI 是否可用（仅检测 Windows 原生）"""
    if DREAMINA_BIN and os.path.exists(DREAMINA_BIN):
        return True
    return False


def _run_dreamina(args, timeout=180):
    """运行 dreamina CLI，自动处理 Windows/WSL 差异"""
    cmd = _dreamina_cmd() + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def load_settings():
    try:
        with open(os.path.join(os.path.dirname(__file__), "settings.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


def save_settings(data):
    with open(os.path.join(os.path.dirname(__file__), "settings.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_video_ratio(ratio_key):
    """将前端比例键值映射到 Dreamina CLI 比例参数"""
    mapping = {
        "16:9": "16:9",
        "9:16": "9:16",
        "1:1": "1:1",
        "4:3": "4:3",
        "3:4": "3:4",
    }
    return mapping.get(ratio_key, "16:9")


def _get_model_version(model_key):
    """将前端模型选择映射到 Dreamina 模型版本"""
    mapping = {
        "seedance2.0": "seedance2.0",
        "seedance2.0fast": "seedance2.0fast",
        "seedance2.0_vip": "seedance2.0_vip",
        "seedance2.0fast_vip": "seedance2.0fast_vip",
    }
    return mapping.get(model_key, "seedance2.0fast")


def _find_settings_file():
    """查找 dreamina settings.json 以确定 token 文件路径"""
    # token 存储在 byted_cli_user_token.json，由 dreamina CLI 管理
    # 我们直接调用 dreamina CLI 即可
    return None



def _get_audio_duration(audio_path):
    """获取音频文件时长（秒）"""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration', '-of', 'csv=p=0', audio_path],
            capture_output=True, text=True, timeout=10
        )
        dur = float(r.stdout.strip()) if r.stdout.strip() else 0
        return dur
    except Exception as e:
        logger.warning(f"获取音频时长失败: {e}")
        return 0


def _trim_audio_if_needed(audio_path, project_name, asset_name):
    """
    检查音频时长是否在 2-15 秒范围内。
    如果太短 (<2s)，循环拼接至 2 秒。
    如果太长 (>15s)，裁剪至 15 秒。
    返回: (trimmed_path, was_trimmed)
    trimmed_path 是原路径或新裁剪路径
    was_trimmed 是否进行了裁剪
    """
    duration = _get_audio_duration(audio_path)
    if 2.0 <= duration <= 15.0:
        return audio_path, False
    
    safe_asset = re.sub(r'[\\/*?:\"<>|]', '_', asset_name)
    base = os.path.basename(audio_path)
    trim_dir = os.path.join(OUTPUT_DIR, re.sub(r'[\\/*?:\"<>|]', '_', project_name), "assets", safe_asset, ".trimmed_audio")
    os.makedirs(trim_dir, exist_ok=True)
    trimmed_path = os.path.join(trim_dir, f"trimmed_{base}")
    
    if duration < 2.0:
        # 循环拼接至至少 2 秒
        loop_count = max(1, int(2.0 / duration) + 1)
        cmd = [
            'ffmpeg', '-y',
            '-stream_loop', str(loop_count),
            '-i', audio_path,
            '-t', '2.0',
            '-ac', '1', '-ar', '16000',
            trimmed_path
        ]
    else:
        # 裁剪至 15 秒
        cmd = [
            'ffmpeg', '-y',
            '-i', audio_path,
            '-t', '15.0',
            '-ac', '1', '-ar', '16000',
            trimmed_path
        ]
    
    try:
        subprocess.run(cmd, capture_output=True, timeout=30, check=True)
        logger.info(f"音频裁剪: {os.path.basename(audio_path)} ({duration:.2f}s -> {trimmed_path})")
        return trimmed_path, True
    except Exception as e:
        logger.warning(f"音频裁剪失败: {e}")
        return audio_path, False


def _find_asset_image(project_name: str, asset_name: str):
    """
    在项目 assets 目录中查找资产图片
    返回: (image_path, error)
    """
    safe_project = re.sub(r'[\\/*?:\"<>|]', '_', project_name)
    safe_asset = re.sub(r'[\\/*?:\"<>|]', '_', asset_name)
    asset_dir = os.path.join(OUTPUT_DIR, safe_project, "assets", safe_asset)
    
    if not os.path.isdir(asset_dir):
        return None, f"资产目录不存在: {asset_dir}"
    
    # 查找最新的 png 文件
    files = sorted([f for f in os.listdir(asset_dir) if f.endswith('.png')])
    if not files:
        return None, f"资产目录中没有图片: {asset_dir}"
    
    return os.path.join(asset_dir, files[-1]), ""


def _load_audio_selections_from_db():
    """从数据库加载音频选择到内存字典"""
    try:
        conn = get_db()
        rows = conn.execute("SELECT project, asset, audio_file FROM audio_selections").fetchall()
        conn.close()
        for row in rows:
            key = f"selected_audio_{row['project']}_{row['asset']}"
            _audio_selections[key] = row['audio_file']
    except Exception as e:
        logger.warning(f"加载音频选择失败: {e}")


# 启动时从数据库加载音频选择
_load_audio_selections_from_db()


def _build_segment_prompt(segment, segment_index, project_name=""):
    """
    构建带 🎞️ Segment X 前缀的提示词
    返回: (prompt_with_prefix, asset_names, asset_selected, asset_audios)
    asset_audios: {asset_name: audio_filename} 仅包含已选音频的资产
    """
    # 提取 segment 标题（去掉时间戳）
    title = segment.get('title', f'Segment {segment_index}')
    
    # 基础提示词
    base_prompt = segment.get('text', '')
    
    # 构建带序号的完整提示词
    prompt = f"🎞️ Segment {segment_index}: {title}\n\n{base_prompt}"
    
    # 提取资产列表
    asset_names = [a.get('name') for a in segment.get('assets', [])]
    asset_selected = segment.get('asset_selected', [True] * len(asset_names))
    
    # 提取已选音频（从 segment 数据中读取）
    asset_audios = {}
    if project_name:
        segment_asset_audios = segment.get('asset_audios', {})
        for i, aname in enumerate(asset_names):
            if i < len(asset_selected) and not asset_selected[i]:
                continue  # 未选中的资产不传音频
            key = f"selected_audio_{project_name}_{aname}"
            if key in _audio_selections:
                asset_audios[aname] = _audio_selections[key]
            elif aname in segment_asset_audios:
                # 兼容旧数据格式
                asset_audios[aname] = segment_asset_audios[aname]
    
    return prompt, asset_names, asset_selected, asset_audios


def _resolve_assets_for_segment(project_name, asset_names, asset_selected, asset_audios=None):
    """
    根据资产名称和选中状态，解析出图片路径和音频路径列表
    返回: (image_paths, audio_paths, error)
    asset_audios: {asset_name: audio_filename} 已选音频映射
    """
    images = []
    audios = []
    if asset_audios is None:
        asset_audios = {}
    for i, name in enumerate(asset_names):
        selected = asset_selected[i] if i < len(asset_selected) else True
        if not selected:
            continue
        # 解析图片
        img_path, err = _find_asset_image(project_name, name)
        if err:
            logger.warning(f"找不到资产图片 {name}: {err}")
            continue
        images.append(img_path)
        # 解析音频（保持与图片索引对齐）
        if name in asset_audios:
            audio_fn = asset_audios[name]
            # 音频文件在同一资产目录下
            safe_name = re.sub(r'[\\/*?:"<>|]', '_', name)
            audio_dir = os.path.join(OUTPUT_DIR, safe_project_re(project_name), "assets", safe_name)
            audio_path = os.path.join(audio_dir, audio_fn)
            if os.path.isfile(audio_path):
                trimmed_path, was_trimmed = _trim_audio_if_needed(audio_path, project_name, name)
                if was_trimmed:
                    logger.info(f"音频已裁剪: {audio_fn}")
                audios.append(trimmed_path)
            else:
                logger.warning(f"音频文件不存在: {audio_path}")
                audios.append(None)
        else:
            audios.append(None)
    return images, audios, ""


def safe_project_re(project_name):
    """安全清理项目名称用于路径"""
    return re.sub(r'[\\/*?:\"<>|]', '_', project_name)


def dreamina_image2video(image_path, prompt, duration=4, model_version="seedance2.0fast", quality="720p"):
    """
    调用 dreamina CLI 生成视频（单图→视频，带资产图片）
    使用 --poll 30 自动轮询等待生成完成
    prompt 必须已带有 🎞️ Segment X 前缀
    返回: (success: bool, submit_id: str, error: str, video_url: str)
    """
    model_mapped = _get_model_version(model_version)
    abs_image = _to_wsl_path(os.path.abspath(image_path))

    cmd = _dreamina_cmd() + [
        "image2video",
        "--image", abs_image,
        "--prompt", prompt,
        "--duration", str(duration),
        "--model_version", model_mapped,
        "--poll", "150",
    ]
    # 只有seedance2.0_vip和seedance2.0fast_vip支持1080p，其他模型只支持720p
    if quality == "1080p" and "vip" in model_version:
        cmd.extend(["--video_resolution", "1080p"])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=180,
            cwd=os.path.expanduser("~"),
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")

        # 解析 JSON 输出，提取 submit_id, gen_status, video_url
        json_start = output.find('{')
        json_end = output.rfind('}') + 1
        if json_start >= 0 and json_end > json_start:
            json_str = output[json_start:json_end]
        else:
            json_str = output

        import json as _json
        try:
            data = _json.loads(json_str)
        except:
            data = {}

        submit_id = data.get("submit_id", "")
        gen_status = data.get("gen_status", "")
        video_url = ""

        # 如果 gen_status 是 success，提取视频 URL
        if gen_status == "success":
            result_json = data.get("result_json", {})
            if isinstance(result_json, str):
                try:
                    result_json = _json.loads(result_json)
                except:
                    pass
            videos = result_json.get("videos", [])
            if videos:
                video_url = videos[0].get("video_url", "")

        if submit_id and (gen_status == "success" or gen_status == "querying"):
            return True, submit_id, "", video_url
        elif gen_status == "fail":
            fail_reason = data.get("fail_reason", "unknown error")
            return False, "", fail_reason, ""
        elif result.returncode != 0:
            return False, "", output[:500], ""
        else:
            return False, "", f"CLI 成功但未解析结果: {output[:200]}", ""
    except subprocess.TimeoutExpired:
        return False, "", "dreamina CLI 超时 (180s)", ""
    except FileNotFoundError:
        return False, "", f"dreamina CLI 未找到: {DREAMINA_BIN}", ""
    except Exception as e:
        return False, "", str(e), ""


def dreamina_multimodal2video(images, prompt, duration=4, ratio="16:9", model_version="seedance2.0fast", quality="720p", audio_files=None, audio_map=None):
    """
    调用 dreamina CLI 生成视频（多图全能参考模式）
    prompt 必须已带有 🎞️ Segment X 前缀
    images: 图片路径列表 [img1, img2, ...]
    audio_files: 音频路径列表 [audio1, audio2, ...] (可选, 与images按索引一一对应)
    audio_map: {image_index: audio_path} 音频映射 (可选, 更精确)
    返回: (success: bool, submit_id: str, error: str, video_url: str)
    """
    model_mapped = _get_model_version(model_version)
    ratio_mapped = _get_video_ratio(ratio)

    cmd = _dreamina_cmd() + [
        "multimodal2video",
        "--model_version", model_mapped,
        "--prompt", prompt,
        "--duration", str(duration),
        "--ratio", ratio_mapped,
    ]
    # 添加图片和音频，按索引一一对应
    if audio_map:
        # 精确映射模式：给出每个图片索引对应的音频
        for idx, img in enumerate(images):
            cmd.extend(["--image", _to_wsl_path(os.path.abspath(img))])
            if idx in audio_map and audio_map[idx]:
                cmd.extend(["--audio", _to_wsl_path(os.path.abspath(audio_map[idx]))])
    elif audio_files:
        # 列表模式：按索引一一对应，None 表示该位无音频
        for idx, img in enumerate(images):
            cmd.extend(["--image", _to_wsl_path(os.path.abspath(img))])
            if idx < len(audio_files) and audio_files[idx]:
                cmd.extend(["--audio", _to_wsl_path(os.path.abspath(audio_files[idx]))])
    else:
        for img in images:
            cmd.extend(["--image", _to_wsl_path(os.path.abspath(img))])
    # 只有seedance2.0_vip和seedance2.0fast_vip支持1080p，其他模型只支持720p
    if quality == "1080p" and "vip" in model_version:
        cmd.extend(["--video_resolution", "1080p"])
    cmd.extend(["--poll", "150"])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=180,
            cwd=os.path.expanduser("~"),
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")

        # 解析 JSON 输出
        json_start = output.find('{')
        json_end = output.rfind('}') + 1
        if json_start >= 0 and json_end > json_start:
            json_str = output[json_start:json_end]
        else:
            json_str = output

        import json as _json
        try:
            data = _json.loads(json_str)
        except:
            data = {}

        submit_id = data.get("submit_id", "")
        gen_status = data.get("gen_status", "")
        video_url = ""

        if gen_status == "success":
            result_json = data.get("result_json", {})
            if isinstance(result_json, str):
                try:
                    result_json = _json.loads(result_json)
                except:
                    pass
            videos = result_json.get("videos", [])
            if videos:
                video_url = videos[0].get("video_url", "")

        if submit_id and (gen_status == "success" or gen_status == "querying"):
            return True, submit_id, "", video_url
        elif gen_status == "fail":
            fail_reason = data.get("fail_reason", "unknown error")
            return False, "", fail_reason, ""
        elif result.returncode != 0:
            return False, "", output[:500], ""
        else:
            return False, "", f"CLI 成功但未解析结果: {output[:200]}", ""
    except subprocess.TimeoutExpired:
        return False, "", "dreamina CLI 超时 (180s)", ""
    except FileNotFoundError:
        return False, "", f"dreamina CLI 未找到: {DREAMINA_BIN}", ""
    except Exception as e:
        return False, "", str(e), ""


def dreamina_query_status(submit_id):
    """
    查询视频生成任务状态
    返回: {status, progress, video_url, error, gen_status}
    status: queued | processing | completed | failed
    """
    cmd = _dreamina_cmd() + [
        "query_result",
        "--submit_id", submit_id,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=60,
            cwd=os.path.expanduser("~"),
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")

        # 提取 JSON
        json_start = output.find('{')
        json_end = output.rfind('}') + 1
        if json_start >= 0 and json_end > json_start:
            json_str = output[json_start:json_end]
        else:
            json_str = output

        import json as _json
        try:
            data = _json.loads(json_str)
        except:
            data = {}

        gen_status = data.get("gen_status", "unknown")
        video_url = ""
        fail_reason = data.get("fail_reason", "")

        # 如果 gen_status 是 success，提取视频 URL
        if gen_status == "success":
            result_json = data.get("result_json", {})
            if isinstance(result_json, str):
                try:
                    result_json = _json.loads(result_json)
                except:
                    pass
            videos = result_json.get("videos", [])
            if videos:
                video_url = videos[0].get("video_url", "")

        # 映射状态
        if gen_status == "success":
            status = "completed"
        elif gen_status == "fail":
            status = "failed"
        elif gen_status == "querying":
            status = "processing"
        else:
            status = gen_status

        return {
            "status": status,
            "progress": 0,
            "video_url": video_url,
            "error": fail_reason if status == "failed" else "",
            "gen_status": gen_status,
        }
    except Exception as e:
        return {"status": "failed", "progress": 0, "video_url": "", "error": str(e), "gen_status": "unknown"}


def download_video(video_url, project_name, episode_num, segment_id):
    """
    从 URL 下载视频到本地
    返回: (local_path, error)
    """
    try:
        import urllib.parse
        safe_proj = re.sub(r'[\\/*?:"<>|]', '_', project_name)
        safe_seg = re.sub(r'[\\/*?:"<>|]', '_', str(segment_id))
        video_dir = os.path.join(OUTPUT_DIR, safe_proj, f"Ep{episode_num:02d}", "videos")
        os.makedirs(video_dir, exist_ok=True)

        # 用 requests 下载
        resp = requests.get(video_url, timeout=120, stream=True)
        if resp.status_code != 200:
            return None, f"下载失败 HTTP {resp.status_code}"

        local_path = os.path.join(video_dir, f"seg_{safe_seg}.mp4")
        with open(local_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return local_path, ""
    except Exception as e:
        return None, str(e)


@app.route("/api/video/generate/dreamina", methods=["POST"])
def generate_video_dreamina():
    """
    使用 Dreamina CLI 生成视频（单并发队列）
    同一时间只允许一个任务运行，其他的排队等待
    请求体:
    {
        episode_id: string,
        segment_ids: [string],
        model: "seedance2.0" | "seedance2.0fast" | "seedance2.0_vip",
        duration: int,
        ratio: string
    }
    """
    data = request.get_json()
    episode_id = data.get("episode_id")
    segment_ids = data.get("segment_ids", [])
    model = data.get("model", "seedance2.0fast")
    duration = int(data.get("duration", 4))
    ratio = data.get("ratio", "16:9")
    quality = data.get("quality", "720p")

    if not episode_id or not segment_ids:
        return jsonify({"error": "缺少参数"}), 400

    conn = get_db()
    ep = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if not ep:
        conn.close()
        return jsonify({"error": "剧集不存在"}), 404

    proj = conn.execute(
        "SELECT name FROM projects WHERE id = ?",
        (ep["project_id"],)
    ).fetchone()
    project_name = proj["name"] if proj else "default"
    conn.close()

    # 从 video_segments 中查找对应的 segment 数据
    ep_data = json.loads(ep["video_segments"]) if ep["video_segments"] else {"segments": []}
    video_segments = ep_data.get("segments", [])
    seg_id_strs = [str(x) for x in segment_ids]
    segments = [s for s in video_segments if str(s.get("id")) in seg_id_strs]

    if not segments:
        return jsonify({"error": "没有可生成的视频提示词，请先推送提示词"}), 400

    # 为每个 segment 构建带资产图片和 🎞️ Segment X 前缀的提示词
    for seg in segments:
        seg_id = seg.get("id")
        seg_index = seg.get("segment_index", seg.get("id"))
        
        # 构建带序号的提示词（传入 project_name 以便读取音频选择）
        prompt, asset_names, asset_selected, asset_audios = _build_segment_prompt(seg, seg_index, project_name)
        
        # 解析资产图片和音频
        asset_images, asset_audio_paths, img_err = _resolve_assets_for_segment(project_name, asset_names, asset_selected, asset_audios)
        
        seg["prompt"] = prompt
        seg["_asset_images"] = asset_images
        seg["_asset_audio"] = asset_audio_paths
        seg["_asset_error"] = img_err
        
        if not asset_images:
            logger.warning(f"Segment {seg_id} 没有可用资产图片")

    task_id = str(uuid.uuid4())[:12]

    with dreamina_tasks_lock:
        dreamina_tasks[task_id] = {
            "status": "queued",
            "total": len(segments),
            "completed": 0,
            "failed": 0,
            "segments": {},
            "episode_id": episode_id,
            "project_name": project_name,
            "episode_num": ep["episode_num"],
            "_model": model,
            "_duration": duration,
            "_ratio": ratio,
            "_quality": quality,
            "_segments": [{"id": s["id"]} for s in segments],  # 保存 segment ID 用于队列回调
        }

    def run_dreamina_generation():
        global dreamina_video_running
        try:
            # 全局单并发：等待锁
            with dreamina_video_queue_lock:
                if dreamina_video_running:
                    logger.info(f"[Dreamina] 已有任务在运行，任务 {task_id} 进入队列")
                    dreamina_video_queue.append(task_id)
                    # 通知前端排队中
                    with dreamina_tasks_lock:
                        dreamina_tasks[task_id]["status"] = "waiting_in_queue"
                        dreamina_tasks[task_id]["queue_position"] = len(dreamina_video_queue)
                else:
                    dreamina_video_running = True

            # 开始处理
            with dreamina_tasks_lock:
                dreamina_tasks[task_id]["status"] = "processing"

            for seg in segments:
                seg_id = seg.get("id")
                prompt = seg.get("prompt", "")
                asset_images = seg.get("_asset_images", [])
                asset_audio_paths = seg.get("_asset_audio", [])
                seg_duration = seg.get("duration", duration)
                seg_model = seg.get("model", model)
                seg_ratio = seg.get("ratio", ratio)
                seg_quality = seg.get("quality", quality)
                
                if not prompt:
                    with dreamina_tasks_lock:
                        dreamina_tasks[task_id]["segments"][seg_id] = {
                            "status": "skipped",
                            "error": "无提示词"
                        }
                    continue

                with dreamina_tasks_lock:
                    dreamina_tasks[task_id]["segments"][seg_id] = {
                        "status": "queued",
                        "prompt": prompt,
                    }

                # 调用 Dreamina CLI - 使用 multimodal2video（多图全能参考模式）
                if asset_images:
                    # 有资产图片：使用 multimodal2video
                    success, submit_id, error, video_url = dreamina_multimodal2video(
                        images=asset_images,
                        prompt=prompt,
                        duration=seg_duration,
                        ratio=seg_ratio,
                        model_version=seg_model,
                        quality=seg_quality,
                        audio_files=asset_audio_paths,
                    )
                else:
                    # 没有资产图片：回退到 image2video（使用第一个资产目录）
                    logger.info(f"[Dreamina] Segment {seg_id} 无资产图片，跳过")
                    with dreamina_tasks_lock:
                        dreamina_tasks[task_id]["segments"][seg_id] = {
                            "status": "skipped",
                            "error": "无可用资产图片"
                        }
                    continue

                if not success:
                    with dreamina_tasks_lock:
                        dreamina_tasks[task_id]["segments"][seg_id] = {
                            "status": "failed",
                            "error": error
                        }
                    with dreamina_tasks_lock:
                        dreamina_tasks[task_id]["failed"] += 1
                    continue

                with dreamina_tasks_lock:
                    dreamina_tasks[task_id]["segments"][seg_id] = {
                        "status": "processing",
                        "submit_id": submit_id,
                        "prompt": prompt,
                    }

                # 如果 --poll 已经拿到了结果，直接标记为 completed
                if video_url:
                    local_path, dl_error = download_video(
                        video_url, project_name, ep["episode_num"], seg_id,
                    )
                    with dreamina_tasks_lock:
                        dreamina_tasks[task_id]["segments"][seg_id] = {
                            "status": "completed",
                            "submit_id": submit_id,
                            "prompt": prompt,
                            "video_url": video_url,
                            "local_path": local_path,
                        }
                        dreamina_tasks[task_id]["completed"] += 1
                    logger.info(f"[Dreamina] segment {seg_id} 生成完成并已下载")
                else:
                    # 否则标记为 processing，等前端轮询
                    pass

            with dreamina_tasks_lock:
                dreamina_tasks[task_id]["status"] = "processing"

        except Exception as e:
            logger.error(f"[Dreamina] 生成任务 {task_id} 出错: {e}")
            with dreamina_tasks_lock:
                dreamina_tasks[task_id]["status"] = "error"
                dreamina_tasks[task_id]["error"] = str(e)
        finally:
            # 释放锁，处理队列中的下一个任务
            with dreamina_video_queue_lock:
                dreamina_video_running = False
                # 处理队列中的下一个
                if dreamina_video_queue:
                    next_task_id = dreamina_video_queue.pop(0)
                    if next_task_id in dreamina_tasks:
                        logger.info(f"[Dreamina] 唤醒队列中的下一个任务: {next_task_id}")
                        t = threading.Thread(
                            target=_continue_next_queued_task,
                            args=(next_task_id,),
                            daemon=True
                        )
                        t.start()

    thread = threading.Thread(target=run_dreamina_generation, daemon=True)
    thread.start()

    return jsonify({
        "task_id": task_id,
        "status": "queued",
        "total": len(segments),
    })


def _continue_next_queued_task(task_id):
    """队列回调：唤醒排队的下一个任务"""
    global dreamina_video_running
    task = dreamina_tasks.get(task_id)
    if not task:
        return
    episode_id = task.get("episode_id")
    segments = task.get("_segments", [])
    model = task.get("_model", "seedance2.0fast")
    duration = task.get("_duration", 4)
    ratio = task.get("_ratio", "16:9")
    quality = task.get("_quality", "720p")

    # 重新构建 segments 的 prompt
    conn = get_db()
    ep = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    conn.close()
    if not ep:
        return
    
    proj = conn.execute("SELECT name FROM projects WHERE id = ?", (ep["project_id"],)).fetchone()
    project_name = proj["name"] if proj else "default"
    
    ep_data = json.loads(ep["video_segments"]) if ep["video_segments"] else {"segments": []}
    video_segments = ep_data.get("segments", [])
    seg_ids = [str(s["id"]) for s in segments]
    segments = [s for s in video_segments if str(s.get("id")) in seg_ids]

    # 为每个 segment 构建带资产图片和 🎞️ Segment X 前缀的提示词
    for seg in segments:
        seg_id = seg.get("id")
        seg_index = seg.get("segment_index", seg_id)
        
        prompt, asset_names, asset_selected, asset_audios = _build_segment_prompt(seg, seg_index, project_name)
        asset_images, asset_audio_paths, img_err = _resolve_assets_for_segment(project_name, asset_names, asset_selected, asset_audios)
        
        seg["prompt"] = prompt
        seg["_asset_images"] = asset_images
        seg["_asset_audio"] = asset_audio_paths
        seg["_asset_error"] = img_err
        
        if not asset_images:
            logger.warning(f"Segment {seg_id} 没有可用资产图片")

    # 重新运行生成逻辑
    def run():
        try:
            with dreamina_tasks_lock:
                dreamina_tasks[task_id]["status"] = "processing"
            for seg in segments:
                seg_id = seg.get("id")
                prompt = seg.get("prompt", "")
                asset_images = seg.get("_asset_images", [])
                asset_audio_paths = seg.get("_asset_audio", [])
                seg_duration = seg.get("duration", duration)
                seg_model = seg.get("model", model)
                seg_ratio = seg.get("ratio", ratio)
                seg_quality = seg.get("quality", quality)
                
                if not prompt:
                    with dreamina_tasks_lock:
                        dreamina_tasks[task_id]["segments"][seg_id] = {
                            "status": "skipped", "error": "无提示词"
                        }
                    continue
                    
                with dreamina_tasks_lock:
                    dreamina_tasks[task_id]["segments"][seg_id] = {
                        "status": "queued", "prompt": prompt
                    }
                
                # 使用 multimodal2video
                if asset_images:
                    success, submit_id, error, video_url = dreamina_multimodal2video(
                        images=asset_images,
                        prompt=prompt,
                        duration=seg_duration,
                        ratio=seg_ratio,
                        model_version=seg_model,
                        quality=seg_quality,
                        audio_files=asset_audio_paths,
                    )
                else:
                    with dreamina_tasks_lock:
                        dreamina_tasks[task_id]["segments"][seg_id] = {
                            "status": "skipped", "error": "无可用资产图片"
                        }
                    continue
                
                if not success:
                    with dreamina_tasks_lock:
                        dreamina_tasks[task_id]["segments"][seg_id] = {
                            "status": "failed", "error": error
                        }
                    with dreamina_tasks_lock:
                        dreamina_tasks[task_id]["failed"] += 1
                    continue
                    
                with dreamina_tasks_lock:
                    dreamina_tasks[task_id]["segments"][seg_id] = {
                        "status": "processing",
                        "submit_id": submit_id,
                        "prompt": prompt,
                        "video_url": video_url
                    }
            with dreamina_tasks_lock:
                dreamina_tasks[task_id]["status"] = "processing"
        except Exception as e:
            logger.error(f"[Dreamina] 队列任务 {task_id} 出错: {e}")
            with dreamina_tasks_lock:
                dreamina_tasks[task_id]["status"] = "error"
                dreamina_tasks[task_id]["error"] = str(e)
        finally:
            with dreamina_video_queue_lock:
                global dreamina_video_running
                dreamina_video_running = False
                if dreamina_video_queue:
                    next_tid = dreamina_video_queue.pop(0)
                    t = threading.Thread(target=_continue_next_queued_task, args=(next_tid,), daemon=True)
                    t.start()
    run()


@app.route("/api/video/dreamina/status/<task_id>", methods=["GET"])
def dreamina_status(task_id):
    """查询 Dreamina 批量生成任务状态"""
    with dreamina_tasks_lock:
        task = dreamina_tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    # 查询每个 segment 的实际状态
    segments_status = {}
    for seg_id, seg_info in task.get("segments", {}).items():
        if seg_info.get("status") in ("completed", "failed", "skipped"):
            segments_status[seg_id] = dict(seg_info)
        elif seg_info.get("submit_id"):
            result = dreamina_query_status(seg_info["submit_id"])
            local_path = None
            video_url = ""

            if result["status"] == "completed" and result.get("video_url"):
                # 下载视频到本地
                local_path, dl_error = download_video(
                    result["video_url"],
                    task["project_name"],
                    task["episode_num"],
                    seg_id,
                )
                if dl_error:
                    logger.warning(f"[Dreamina] 下载视频失败: {dl_error}")

                with dreamina_tasks_lock:
                    if seg_id in dreamina_tasks.get(task_id, {}).get("segments", {}):
                        dreamina_tasks[task_id]["segments"][seg_id]["status"] = "completed"
                        dreamina_tasks[task_id]["segments"][seg_id]["video_url"] = result["video_url"]
                        dreamina_tasks[task_id]["segments"][seg_id]["local_path"] = local_path
                        dreamina_tasks[task_id]["completed"] += 1

                segments_status[seg_id] = {
                    "status": "completed",
                    "video_url": result["video_url"],
                    "local_path": local_path,
                }
            elif result["status"] == "failed":
                with dreamina_tasks_lock:
                    if seg_id in dreamina_tasks.get(task_id, {}).get("segments", {}):
                        dreamina_tasks[task_id]["segments"][seg_id]["status"] = "failed"
                        dreamina_tasks[task_id]["segments"][seg_id]["error"] = result.get("error", "")
                        dreamina_tasks[task_id]["failed"] += 1

                segments_status[seg_id] = {
                    "status": "failed",
                    "error": result.get("error", ""),
                }
            else:
                # processing / queued
                with dreamina_tasks_lock:
                    if seg_id in dreamina_tasks.get(task_id, {}).get("segments", {}):
                        dreamina_tasks[task_id]["segments"][seg_id]["status"] = result["status"]
                segments_status[seg_id] = {
                    "status": result["status"],
                    "progress": result["progress"],
                    "submit_id": seg_info.get("submit_id"),
                }

    completed = task.get("completed", 0)
    failed = task.get("failed", 0)
    total = task.get("total", 0)

    if completed + failed >= total:
        overall = "completed" if failed == 0 else "partial"
    else:
        overall = "processing"

    return jsonify({
        "task_id": task_id,
        "status": overall,
        "total": total,
        "completed": completed,
        "failed": failed,
        "segments": segments_status,
        "error": task.get("error", ""),
    })


@app.route("/api/video/dreamina/result/<task_id>/<seg_id>", methods=["GET"])
def dreamina_result(task_id, seg_id):
    """获取单个 segment 的视频生成结果"""
    with dreamina_tasks_lock:
        task = dreamina_tasks.get(task_id, {})
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    seg = task.get("segments", {}).get(seg_id, {})
    return jsonify(seg)


@app.route("/api/settings/dreamina-models", methods=["GET"])
def get_dreamina_models():
    """获取可用的 Dreamina 模型列表"""
    return jsonify({
        "models": [
            {"key": "seedance2.0", "name": "Seedance 2.0 (720p) 非VIP", "default_duration": 4},
            {"key": "seedance2.0fast", "name": "Seedance 2.0 Fast (720p) 非VIP ★推荐", "default_duration": 4},
            {"key": "seedance2.0_vip", "name": "Seedance 2.0 VIP (720p/1080p)", "default_duration": 4},
            {"key": "seedance2.0fast_vip", "name": "Seedance 2.0 Fast VIP (720p/1080p)", "default_duration": 4},
        ]
    })


@app.route("/api/settings/video-generation", methods=["GET"])
def get_video_gen_settings():
    """获取视频生成设置（模式、模型等）"""
    s = load_settings()
    return jsonify({
        "mode": s.get("videoGenMode", "dreamina"),  # "dreamina" | "api"
        "model": s.get("videoGenModel", "seedance2.0fast"),
        "duration": s.get("videoGenDuration", 4),
        "ratio": s.get("videoGenRatio", "16:9"),
        "dreamina_available": _dreamina_available(),
    })


@app.route("/api/settings/video-generation", methods=["POST"])
def save_video_gen_settings():
    """保存视频生成设置"""
    data = request.get_json()
    s = load_settings()
    s["videoGenMode"] = data.get("mode", "dreamina")
    s["videoGenModel"] = data.get("model", "seedance2.0fast")
    s["videoGenDuration"] = int(data.get("duration", 4))
    s["videoGenRatio"] = data.get("ratio", "16:9")
    save_settings(s)
    return jsonify({"ok": True})

# ====== Dreamina 登录/状态管理 ======

# Hermes 二进制路径：优先环境变量，其次项目目录，最后默认路径
HERMES_BIN = os.environ.get("HERMES_BIN", "")
if not HERMES_BIN or not os.path.exists(HERMES_BIN):
    local_hermes = os.path.join(os.path.dirname(__file__), "..", "hermes", "hermes.exe")
    if os.path.exists(local_hermes):
        HERMES_BIN = os.path.abspath(local_hermes)
if not HERMES_BIN or not os.path.exists(HERMES_BIN):
    HERMES_BIN = r"D:\hermes\hermes-agent\venv\Scripts\hermes.exe"  # 开发者本机兜底

def _dreamina_run_to_file(args: str) -> str:
    """运行 dreamina 命令，输出重定向到临时文件（避免 Windows PIPE 缓冲问题）。
    返回文件内容字符串。"""
    import os, time, uuid
    # 项目目录下建临时文件，避免系统 Temp 目录的路径问题
    base = os.path.join(os.path.dirname(__file__), f"_dm_{uuid.uuid4().hex[:8]}")
    tp = base + ".txt"
    bat = base + ".bat"
    try:
        with open(bat, 'w', encoding='gbk' if os.name == 'nt' else 'utf-8') as f:
            f.write(f'@"{DREAMINA_BIN}" {args} > "{tp}" 2>&1\n')
        p = subprocess.Popen(f'cmd.exe /c "{bat}"', shell=True)
        dl = time.time() + 10
        while time.time() < dl:
            if p.poll() is not None: break
            time.sleep(0.2)
        if p.poll() is None: p.kill()
        time.sleep(0.3)
        with open(tp, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except Exception:
        return ""
    finally:
        for f in (tp, bat):
            try: os.unlink(f)
            except: pass


def _dreamina_logged_in():
    """检查 dreamina 是否已登录"""
    if not DREAMINA_BIN:
        return False
    # user_credit 成功时返回 0，失败时也不阻塞
    import tempfile, os, time
    tf = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
    tp = tf.name; tf.close()
    bat = tp + '.bat'
    try:
        import locale
        # Windows cmd.exe 读 bat 用系统 OEM 编码（中文 = gbk/cp936）
        bat_enc = 'gbk' if os.name == 'nt' else 'utf-8'
        with open(bat, 'w', encoding=bat_enc) as f:
            f.write(f'@"{DREAMINA_BIN}" user_credit > "{tp}" 2>&1\n')
        p = subprocess.Popen(f'cmd.exe /c "{bat}"', shell=True)
        dl = time.time() + 8
        while time.time() < dl:
            if p.poll() is not None: break
            time.sleep(0.2)
        if p.poll() is None: p.kill()
        time.sleep(0.2)
        return p.poll() == 0
    except Exception:
        return False
    finally:
        try: os.unlink(tp)
        except: pass
        try: os.unlink(bat)
        except: pass


@app.route("/api/dreamina/login-status", methods=["GET"])
def dreamina_login_status():
    """获取 dreamina 登录状态和余额"""
    has_bin = bool(DREAMINA_BIN) if DREAMINA_BIN else _dreamina_available()
    logged_in = _dreamina_logged_in() if has_bin else False
    credit = ""
    if logged_in:
        credit = _dreamina_run_to_file("user_credit").strip()
    return jsonify({
        "available": has_bin,
        "logged_in": logged_in,
        "credit": credit,
        "bin_path": DREAMINA_BIN,
    })


@app.route("/api/dreamina/login/start", methods=["POST"])
def dreamina_login_start():
    """启动 OAuth Device Flow 登录，返回 device_code 和 user_code"""
    if not DREAMINA_BIN:
        return jsonify({"error": "dreamina CLI 未找到"}), 500

    output = ""
    output = _dreamina_run_to_file("login --headless")

    if not output:
        return jsonify({"error": "无法获取登录码，请确认网络连接正常"}), 500

    # 如果已登录（"已复用当前本地 OAuth 登录态"），直接返回成功
    if "已复用" in output or "已登录" in output:
        return jsonify({
            "already_logged_in": True,
            "verification_uri": "",
            "user_code": "",
            "device_code": "",
        })

    # 解析输出
    import re as _re
    verification_uri = ""
    user_code = ""
    device_code = ""
    for line in output.split('\n'):
        line = line.strip()
        if line.startswith("verification_uri:"):
            verification_uri = line.split(":", 1)[1].strip()
        elif line.startswith("user_code:"):
            user_code = line.split(":", 1)[1].strip()
        elif line.startswith("device_code:"):
            device_code = line.split(":", 1)[1].strip()

    if not device_code:
        return jsonify({"error": "无法获取 device_code", "raw": output}), 500

    return jsonify({
        "verification_uri": verification_uri,
        "user_code": user_code,
        "device_code": device_code,
    })


@app.route("/api/dreamina/login/poll", methods=["POST"])
def dreamina_login_poll():
    """检查登录是否完成（通过 user_credit 快速检测）"""
    if not DREAMINA_BIN:
        return jsonify({"error": "dreamina CLI 未找到"}), 500
    data = request.get_json()
    device_code = data.get("device_code", "")
    # device_code 仅用于日志，实际用 user_credit 检测
    output = _dreamina_run_to_file("user_credit")
    success = "credit" in output.lower() or "余额" in output
    return jsonify({"success": success, "output": output[:500]})


@app.route("/api/dreamina/logout", methods=["POST"])
def dreamina_logout():
    """清除 dreamina 登录态（删除 token 文件）"""
    import glob as _glob
    removed = []
    # 尝试删除可能的 token 文件位置
    token_paths = [
        os.path.expanduser("~/.local/share/dreamina/byted_cli_user_token.json"),
        os.path.expanduser("~/.dreamina_cli/byted_cli_user_token.json"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "dreamina", "byted_cli_user_token.json"),
        os.path.join(os.environ.get("APPDATA", ""), "dreamina", "byted_cli_user_token.json"),
    ]
    for p in token_paths:
        try:
            if os.path.exists(p):
                os.remove(p)
                removed.append(p)
        except Exception:
            pass
    return jsonify({"ok": True, "removed": removed})


# ====== 自动关闭：浏览器关 Tab 时退出后台进程 ======

_last_heartbeat = time.time()
_heartbeat_lock = threading.Lock()

@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    """前端心跳，用于检测 Tab 是否关闭"""
    global _last_heartbeat
    with _heartbeat_lock:
        _last_heartbeat = time.time()
    return jsonify({"ok": True})

@app.route("/api/shutdown", methods=["POST"])
def shutdown():
    """前端关闭时调用，优雅退出"""
    save_all_to_disk()
    # 在另一个线程中退出，先返回响应
    def _quit():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_quit, daemon=True).start()
    return jsonify({"ok": True})

def _heartbeat_watcher():
    """后台线程：超过 60 秒无心跳则退出"""
    while True:
        time.sleep(10)
        with _heartbeat_lock:
            gap = time.time() - _last_heartbeat
        if gap > 60:
            print(f"心跳超时 ({gap:.0f}s)，自动退出")
            save_all_to_disk()
            os._exit(0)


# ====== Hermes API 配置管理 ======

HERMES_CONFIG_PATHS = [
    os.path.expanduser("~/.hermes/profiles/storyboard/config.yaml"),
    os.path.expanduser("~/.hermes/profiles/asset-designer/config.yaml"),
    os.path.expanduser("~/.hermes/profiles/seedance-prompt/config.yaml"),
]

CONFIG_TEMPLATE = os.path.join(os.path.dirname(__file__), "config_template.yaml")


API_CONFIG_FILE = os.path.join(os.environ.get("USER_DATA") or os.path.join(os.environ.get("APPDATA", os.path.dirname(__file__)), "Juben"), "hermes_api.json")

def _get_hermes_config():
    """读取持久化的 Hermes API 配置"""
    if os.path.exists(API_CONFIG_FILE):
        try:
            with open(API_CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            cfg["configured"] = bool(cfg.get("api_key") and cfg["api_key"] != "YOUR_API_KEY_HERE")
            return cfg
        except: pass
    return {"api_key": "", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-pro", "provider": "deepseek", "configured": False}


def _save_hermes_config(api_key, base_url, model_name, provider):
    """保存到持久化文件 + 同步到所有已解密 profile"""
    cfg = {"api_key": api_key, "base_url": base_url, "model": model_name, "provider": provider}
    # 保存到持久文件
    with open(API_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False)
    # 同步到 profile config
    for prof in ["storyboard", "asset-designer", "seedance-prompt"]:
        home = os.environ.get("HERMES_HOME", "")
        if home and os.path.isdir(home):
            prof_path = os.path.join(home, prof)
        else:
            prof_path = os.path.expanduser(f"~/.hermes/profiles/{prof}")
        cfg_path = os.path.join(prof_path, "config.yaml")
        if os.path.exists(cfg_path):
            try:
                import yaml
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                if "model" not in data: data["model"] = {}
                data["model"].update(cfg)
                with open(cfg_path, 'w', encoding='utf-8') as f:
                    yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
            except: pass
    return ["saved"]


@app.route("/api/settings/hermes-config", methods=["GET"])
def get_hermes_config():
    return jsonify(_get_hermes_config())


@app.route("/api/settings/hermes-config", methods=["POST"])
def save_hermes_config():
    data = request.get_json()
    api_key = (data.get("api_key") or "").strip()
    base_url = (data.get("base_url") or "https://api.deepseek.com/v1").strip()
    model_name = (data.get("model") or "deepseek-v4-pro").strip()
    provider = (data.get("provider") or "deepseek").strip()

    if not api_key:
        return jsonify({"error": "API Key 不能为空"}), 400

    # 先测试连接
    import urllib.request as _ur
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    test_url = base + "/v1/chat/completions"
    try:
        req = _ur.Request(test_url, method="POST")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        body = json.dumps({
            "model": model_name,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5,
        }).encode("utf-8")
        resp = _ur.urlopen(req, data=body, timeout=15)
        resp.read()  # consume response
    except _ur.HTTPError as e:
        if e.code == 401:
            return jsonify({"error": "API Key 无效（401）"}), 400
        if e.code == 404:
            return jsonify({"error": f"接口不存在（404），请检查 API 地址"}), 400
        err_body = ""
        try: err_body = e.read().decode()[:200]
        except: pass
        return jsonify({"error": f"API 错误 {e.code}: {err_body}"}), 400
    except Exception as e:
        msg = str(e)
        if "timed out" in msg.lower():
            return jsonify({"error": "连接超时，请检查 API 地址是否正确"}), 400
        return jsonify({"error": f"连接失败: {msg}"}), 400

    result = _save_hermes_config(api_key, base_url, model_name, provider)
    return jsonify({"ok": True, "updated": result})


if __name__ == "__main__":
    if not _validate_js():
        import sys; sys.exit(1)
    port = int(os.environ.get("PORT", 5000))
    save_all_to_disk()
    # 启动心跳监控线程
    threading.Thread(target=_heartbeat_watcher, daemon=True).start()
    print(f"Storyboard App v4 启动: http://localhost:{port}")
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=8,
              channel_timeout=7200, cleanup_interval=60)
    except OSError:
        # waitress socket 耗尽时回退到 Flask dev server
        print("waitress 启动失败，使用 Flask 内置服务器")
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
