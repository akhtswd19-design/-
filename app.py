import os, json, threading, time
from datetime import datetime
from flask import Flask, jsonify, render_template, request, abort, Response
import telebot
import requests
from telebot import types
from sqlalchemy import create_engine, text

BOT_TOKEN = os.getenv('BOT_TOKEN', '8843571560:AAEmeIzqgZF5MMeGAsbXz6mfOnnsO7U4Unk').strip()
ADMIN_ID = int(os.getenv('ADMIN_ID', '1056007305'))
WEBAPP_URL = os.getenv('WEBAPP_URL', 'https://3y7p5gzhe8.onrender.com').rstrip('/')
# Approximate display conversion only. Set this yourself; Telegram does not define a fixed SAR/Star rate.
SAR_PER_STAR = float(os.getenv('SAR_PER_STAR', '1.00'))
DB_URL = os.getenv('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
if not DB_URL:
    DB_URL = 'sqlite:///alaqil.db'
engine = create_engine(DB_URL, future=True, pool_pre_ping=True)
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML') if BOT_TOKEN else None
admin_steps = {}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS customers (user_id BIGINT PRIMARY KEY, first_name TEXT, username TEXT, joined_at TEXT);
CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, price_sar REAL NOT NULL, stars INTEGER NOT NULL, description TEXT NOT NULL, photo_file_id TEXT, group_url TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, user_id BIGINT NOT NULL, product_id INTEGER NOT NULL, stars INTEGER NOT NULL, status TEXT NOT NULL, charge_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY, user_id BIGINT NOT NULL, name TEXT, rating INTEGER NOT NULL, review TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0, created_at TEXT);
'''
with engine.begin() as c:
    for stmt in SCHEMA.split(';'):
        if stmt.strip(): c.execute(text(stmt))

def db(sql, params=None, one=False, all=False):
    with engine.begin() as c:
        r = c.execute(text(sql), params or {})
        if one: return r.mappings().first()
        if all: return [dict(x) for x in r.mappings().all()]
        return r.rowcount

def stars_for(sar):
    return max(1, int(round(float(sar) / SAR_PER_STAR)))

def is_admin(uid): return ADMIN_ID and int(uid) == ADMIN_ID

def notify_admin(msg):
    if bot and ADMIN_ID:
        try: bot.send_message(ADMIN_ID, msg)
        except Exception: pass

@app.get('/')
def home(): return render_template('index.html', app_title='متجر العاقل')

@app.get('/health')
def health(): return {'ok': True}

@app.get('/api/products')
def products():
    rows = db('SELECT id,name,price_sar,stars,description,photo_file_id FROM products WHERE active=1 ORDER BY id DESC', all=True)
    for p in rows: p['image'] = f"/image/{p['id']}" if p['photo_file_id'] else None
    return jsonify(rows)

@app.get('/api/reviews')
def reviews():
    return jsonify(db('SELECT id,name,rating,review,created_at FROM reviews WHERE approved=1 ORDER BY id DESC LIMIT 50', all=True))

@app.post('/api/invoice')
def create_invoice():
    if not bot: return jsonify(error='البوت غير مضبوط بعد'), 503
    data = request.get_json(silent=True) or {}
    try: uid = int(data.get('user_id', 0)); pid = int(data.get('product_id', 0))
    except (TypeError, ValueError): return jsonify(error='طلب غير صالح'), 400
    if not uid: return jsonify(error='افتح المتجر من داخل تيليجرام'), 400
    p = db('SELECT * FROM products WHERE id=:id AND active=1', {'id':pid}, one=True)
    if not p: return jsonify(error='المنتج غير موجود'), 404
    payload = f"alaqil:{p['id']}:{uid}:{int(time.time())}"
    try:
        url = bot.create_invoice_link(title=p['name'][:32], description=p['description'][:255],
            invoice_payload=payload, provider_token='', currency='XTR',
            prices=[types.LabeledPrice(label=p['name'][:32], amount=int(p['stars']))])
        db('INSERT INTO orders(user_id,product_id,stars,status,created_at) VALUES(:u,:p,:s,:st,:t)',
           {'u':uid,'p':pid,'s':p['stars'],'st':'pending','t':datetime.utcnow().isoformat()})
        return jsonify(url=url)
    except Exception as e:
        app.logger.exception('invoice creation failed')
        return jsonify(error='تعذر إنشاء الفاتورة، حاول لاحقًا'), 500

@app.get('/image/<int:pid>')
def product_image(pid):
    p = db('SELECT photo_file_id FROM products WHERE id=:id AND active=1', {'id':pid}, one=True)
    if not p or not p['photo_file_id'] or not bot: abort(404)
    try:
        f = bot.get_file(p['photo_file_id'])
        response = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}", timeout=15)
        response.raise_for_status()
        return Response(response.content, content_type=response.headers.get('Content-Type', 'image/jpeg'), headers={'Cache-Control':'public, max-age=3600'})
    except Exception:
        app.logger.exception('image proxy failed')
        abort(404)

@app.post('/api/review')
def add_review():
    data=request.get_json(silent=True) or {}
    try: uid=int(data.get('user_id')); rating=int(data.get('rating'))
    except (ValueError, TypeError): return jsonify(error='بيانات غير صحيحة'),400
    review=(data.get('review') or '').strip()[:500]
    name=(data.get('name') or 'عميل').strip()[:80]
    if not 1 <= rating <= 5 or len(review)<3: return jsonify(error='اكتب تقييمًا من 3 أحرف على الأقل'),400
    db('INSERT INTO reviews(user_id,name,rating,review,approved,created_at) VALUES(:u,:n,:r,:v,0,:t)',
       {'u':uid,'n':name,'r':rating,'v':review,'t':datetime.utcnow().isoformat()})
    notify_admin(f"⭐ تقييم جديد ينتظر المراجعة\nمن: {name}\nالتقييم: {rating}/5\n{review}\nاستخدم /admin لمراجعته.")
    return jsonify(ok=True)

if bot:
    @bot.message_handler(commands=['start'])
    def start(m):
        u=m.from_user
        db('INSERT INTO customers(user_id,first_name,username,joined_at) VALUES(:u,:n,:un,:t) ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username',
           {'u':u.id,'n':u.first_name or '', 'un':u.username or '', 't':datetime.utcnow().isoformat()})
        kb=types.InlineKeyboardMarkup()
        if WEBAPP_URL: kb.add(types.InlineKeyboardButton('فتح متجر العاقل ✦', web_app=types.WebAppInfo(url=WEBAPP_URL)))
        bot.send_message(m.chat.id, 'أهلًا بك في <b>متجر العاقل</b> ✦\nملفات رقمية وتسليم بعد تأكيد الدفع.', reply_markup=kb)

    @bot.message_handler(commands=['admin'])
    def admin(m):
        if not is_admin(m.from_user.id): return bot.reply_to(m,'هذا الأمر خاص بالإدارة.')
        kb=types.InlineKeyboardMarkup(row_width=2)
        kb.add(types.InlineKeyboardButton('➕ إضافة منتج',callback_data='add'), types.InlineKeyboardButton('📦 المنتجات',callback_data='manage'))
        kb.add(types.InlineKeyboardButton('📊 الإحصائيات',callback_data='stats'), types.InlineKeyboardButton('📣 رسالة جماعية',callback_data='broadcast'))
        kb.add(types.InlineKeyboardButton('⭐ التقييمات',callback_data='reviews'))
        bot.send_message(m.chat.id,'<b>لوحة تحكم العاقل</b>',reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data in ['add','manage','stats','broadcast','reviews','cancel','approve_review','delete_review'] or c.data.startswith(('delp:','editp:','review:','delete_review:')))
    def callbacks(c):
        if not is_admin(c.from_user.id): return bot.answer_callback_query(c.id,'غير مصرح')
        uid=c.from_user.id
        if c.data=='cancel':
            admin_steps.pop(uid,None); bot.answer_callback_query(c.id,'تم الإلغاء')
        elif c.data=='add':
            admin_steps[uid]={'step':'name'}; bot.send_message(uid,'أرسل اسم المنتج:')
        elif c.data=='manage':
            rows=db('SELECT id,name,price_sar,active FROM products ORDER BY id DESC',all=True)
            if not rows: return bot.send_message(uid,'لا توجد منتجات.')
            for p in rows:
                kb=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton('تعديل',callback_data=f"editp:{p['id']}"), types.InlineKeyboardButton('حذف/إخفاء',callback_data=f"delp:{p['id']}"))
                bot.send_message(uid,f"#{p['id']} — {p['name']} — {p['price_sar']} ر.س — {'نشط' if p['active'] else 'مخفي'}",reply_markup=kb)
        elif c.data.startswith('editp:'):
            pid=int(c.data.split(':')[1]); p=db('SELECT * FROM products WHERE id=:id',{'id':pid},one=True)
            if not p: return bot.answer_callback_query(c.id,'المنتج غير موجود')
            admin_steps[uid]={'step':'edit_name','id':pid,'name':p['name'],'price':p['price_sar'],'stars':p['stars'],'description':p['description'],'photo':p['photo_file_id'],'group':p['group_url']}
            bot.send_message(uid, f"تعديل المنتج #{pid}. أرسل الاسم الجديد أو أرسل - للإبقاء على الحالي ({p['name']}):")
        elif c.data.startswith('delp:'):
            pid=int(c.data.split(':')[1]); db('UPDATE products SET active=0 WHERE id=:id',{'id':pid}); bot.answer_callback_query(c.id,'تم إخفاء المنتج'); bot.send_message(uid,'تم إخفاء المنتج من المتجر.')
        elif c.data=='stats':
            p=db('SELECT COUNT(*) n FROM products WHERE active=1',one=True)['n']; o=db("SELECT COUNT(*) n FROM orders WHERE status='paid'",one=True)['n']; pend=db("SELECT COUNT(*) n FROM orders WHERE status='pending'",one=True)['n']; cust=db('SELECT COUNT(*) n FROM customers',one=True)['n']
            bot.send_message(uid,f'📊 <b>الإحصائيات</b>\nالمنتجات النشطة: {p}\nالعملاء: {cust}\nالطلبات المدفوعة: {o}\nطلبات بانتظار الدفع: {pend}')
        elif c.data=='broadcast': admin_steps[uid]={'step':'broadcast'}; bot.send_message(uid,'أرسل نص الرسالة الجماعية الآن. لإلغاء العملية أرسل /cancel')
        elif c.data=='reviews':
            rows=db('SELECT id,name,rating,review FROM reviews WHERE approved=0 ORDER BY id DESC LIMIT 20',all=True)
            if not rows: return bot.send_message(uid,'لا توجد تقييمات معلقة.')
            for r in rows:
                kb=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton('إظهار',callback_data=f"review:{r['id']}"),types.InlineKeyboardButton('حذف',callback_data=f"delete_review:{r['id']}"))
                bot.send_message(uid,f"#{r['id']} — {r['name']} — {r['rating']}/5\n{r['review']}",reply_markup=kb)
        elif c.data.startswith('review:'):
            rid=int(c.data.split(':')[1]); db('UPDATE reviews SET approved=1 WHERE id=:id',{'id':rid}); bot.answer_callback_query(c.id,'تم اعتماد التقييم')
        elif c.data.startswith('delete_review:'):
            rid=int(c.data.split(':')[1]); db('DELETE FROM reviews WHERE id=:id',{'id':rid}); bot.answer_callback_query(c.id,'تم حذف التقييم')

    @bot.message_handler(commands=['cancel'])
    def cancel(m):
        if is_admin(m.from_user.id): admin_steps.pop(m.from_user.id,None); bot.reply_to(m,'تم الإلغاء.')

    @bot.message_handler(content_types=['text','photo'])
    def admin_flow(m):
        uid=m.from_user.id
        if not is_admin(uid) or uid not in admin_steps: return
        state=admin_steps[uid]; step=state['step']
        if step.startswith('edit_'):
            value = (m.text or '').strip() if m.content_type == 'text' else ''
            if step == 'edit_name':
                if value and value != '-': state['name'] = value[:100]
                state['step']='edit_price'; return bot.reply_to(m, f"أرسل السعر الجديد بالريال أو - للإبقاء على {state['price']}:")
            if step == 'edit_price':
                if value and value != '-':
                    try: price=float(value.replace(',','.'))
                    except ValueError: return bot.reply_to(m,'أرسل رقمًا صحيحًا أو - للإبقاء على السعر.')
                    if price <= 0: return bot.reply_to(m,'السعر يجب أن يكون أكبر من صفر.')
                    state['price']=price; state['stars']=stars_for(price)
                state['step']='edit_description'; return bot.reply_to(m,'أرسل الوصف الجديد أو - للإبقاء على الحالي:')
            if step == 'edit_description':
                if value and value != '-': state['description']=value[:1000]
                state['step']='edit_photo'; return bot.reply_to(m,'أرسل صورة جديدة أو أرسل - للإبقاء على الصورة الحالية:')
            if step == 'edit_photo':
                if m.photo: state['photo']=m.photo[-1].file_id
                elif value and value != '-': return bot.reply_to(m,'أرسل صورة أو - للإبقاء على الصورة.')
                state['step']='edit_group'; return bot.reply_to(m,'أرسل رابط التسليم الجديد أو - للإبقاء على الحالي:')
            if step == 'edit_group':
                if value and value != '-': state['group']=value
                db('UPDATE products SET name=:n,price_sar=:p,stars=:st,description=:d,photo_file_id=:ph,group_url=:g,active=1 WHERE id=:id', {'n':state['name'],'p':state['price'],'st':state['stars'],'d':state['description'],'ph':state['photo'],'g':state['group'],'id':state['id']})
                admin_steps.pop(uid,None); return bot.reply_to(m, f"تم تحديث المنتج #{state['id']} بنجاح. السعر: {state['price']} ر.س / {state['stars']} نجمة.")
        if step=='broadcast':
            textmsg=m.text or m.caption or ''
            if not textmsg: return bot.reply_to(m,'أرسل نصًا أو ألغِ العملية.')
            users=db('SELECT user_id FROM customers',all=True); sent=failed=0
            for x in users:
                try: bot.send_message(x['user_id'],textmsg); sent+=1
                except Exception: failed+=1
                time.sleep(0.04)
            admin_steps.pop(uid,None); return bot.reply_to(m,f'انتهى الإرسال. نجح: {sent}، تعذر: {failed}.')
        if step=='name' and m.text:
            state['name']=m.text[:100]; state['step']='price'; bot.reply_to(m,'أرسل السعر بالريال السعودي (مثال: 20):')
        elif step=='price' and m.text:
            try: price=float(m.text.replace(',','.'))
            except ValueError: return bot.reply_to(m,'أرسل رقمًا صحيحًا للسعر.')
            if price<=0: return bot.reply_to(m,'السعر يجب أن يكون أكبر من صفر.')
            state['price']=price; state['stars']=stars_for(price); state['step']='description'
            bot.reply_to(m,f"السعر التقريبي: {state['stars']} نجمة حسب إعداد SAR_PER_STAR.\nأرسل وصف المنتج:")
        elif step=='description' and m.text:
            state['description']=m.text[:1000]; state['step']='photo'; bot.reply_to(m,'أرسل صورة المنتج كصورة داخل المحادثة (وليس كرابط).')
        elif step=='photo' and m.photo:
            state['photo']=m.photo[-1].file_id; state['step']='group'; bot.reply_to(m,'أرسل رابط القروب/الملفات الذي سيُسلّم بعد الدفع:')
        elif step=='photo': bot.reply_to(m,'أرسل الصورة كمرفق صورة في تيليجرام.')
        elif step=='group' and m.text:
            state['group']=m.text.strip(); state['step']='confirm'
            kb=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton('حفظ المنتج',callback_data='saveproduct'),types.InlineKeyboardButton('إلغاء',callback_data='cancel'))
            bot.send_message(uid,f"راجع المنتج:\nالاسم: {state['name']}\nالسعر: {state['price']} ر.س / {state['stars']} نجمة\nالوصف: {state['description']}\nالرابط: {state['group']}",reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data in ['saveproduct','cancel'])
    def save_product(c):
        if not is_admin(c.from_user.id): return bot.answer_callback_query(c.id,'غير مصرح')
        uid=c.from_user.id; s=admin_steps.get(uid)
        if c.data=='cancel':
            admin_steps.pop(uid,None); return bot.answer_callback_query(c.id,'تم الإلغاء')
        if not s or s.get('step')!='confirm': return bot.answer_callback_query(c.id,'انتهت الجلسة، أعد المحاولة')
        db('INSERT INTO products(name,price_sar,stars,description,photo_file_id,group_url,active,created_at) VALUES(:n,:p,:st,:d,:ph,:g,1,:t)',
           {'n':s['name'],'p':s['price'],'st':s['stars'],'d':s['description'],'ph':s['photo'],'g':s['group'],'t':datetime.utcnow().isoformat()})
        admin_steps.pop(uid,None); bot.answer_callback_query(c.id,'تم الحفظ')
        bot.send_message(uid,'تمت إضافة المنتج بنجاح.')
        # New-product announcement to known customers, excluding admin.
        users=db('SELECT user_id FROM customers',all=True)
        for x in users:
            if int(x['user_id'])==ADMIN_ID: continue
            try: bot.send_message(x['user_id'],f"🆕 منتج جديد في متجر العاقل: <b>{s['name']}</b>\nالسعر: {s['price']} ر.س / {s['stars']} نجمة\nافتح المتجر للاطلاع عليه.")
            except Exception: pass

    @bot.pre_checkout_query_handler(func=lambda q: True)
    def pre_checkout(q):
        try:
            parts=q.invoice_payload.split(':')
            if len(parts)!=4 or parts[0]!='alaqil' or int(parts[2])!=q.from_user.id: return bot.answer_pre_checkout_query(q.id,ok=False,error_message='الفاتورة غير صالحة.')
            p=db('SELECT id,stars,active FROM products WHERE id=:id',{'id':int(parts[1])},one=True)
            if not p or not p['active'] or int(p['stars'])!=q.total_amount or q.currency!='XTR': return bot.answer_pre_checkout_query(q.id,ok=False,error_message='تعذر التحقق من المنتج والسعر.')
            bot.answer_pre_checkout_query(q.id,ok=True)
        except Exception: bot.answer_pre_checkout_query(q.id,ok=False,error_message='تعذر التحقق من الطلب.')

    @bot.message_handler(content_types=['successful_payment'])
    def successful_payment(m):
        sp=m.successful_payment; parts=sp.invoice_payload.split(':')
        if len(parts)!=4 or parts[0]!='alaqil': return
        pid=int(parts[1]); uid=m.from_user.id
        db("UPDATE orders SET status='paid',charge_id=:ch WHERE user_id=:u AND product_id=:p AND status='pending'",{'ch':sp.telegram_payment_charge_id,'u':uid,'p':pid})
        p=db('SELECT name,group_url FROM products WHERE id=:id',{'id':pid},one=True)
        if p:
            bot.send_message(uid,f"✅ تم الدفع بنجاح!\nالمنتج: <b>{p['name']}</b>\nرابط التسليم: {p['group_url']}\nاحتفظ بهذه الرسالة للرجوع إليها.")
            notify_admin(f"💰 عملية شراء ناجحة\nالعميل: {uid}\nالمنتج: {p['name']}\nالمبلغ: {sp.total_amount} نجمة")

    @bot.message_handler(commands=['paysupport'])
    def paysupport(m): bot.reply_to(m,'للدعم بخصوص الدفع، تواصل مع إدارة متجر العاقل.')

def run_bot():
    if bot:
        while True:
            try: bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
            except Exception:
                app.logger.exception('Bot polling stopped; retrying')
                time.sleep(5)

_bot_thread = None
if bot:
    _bot_thread = threading.Thread(target=run_bot, daemon=True, name='telegram-bot-polling')
    _bot_thread.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT','10000')))
