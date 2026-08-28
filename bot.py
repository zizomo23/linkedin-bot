import os
import asyncio
import logging
import re
import time
import html
import json
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram.error import Forbidden, BadRequest

TOKEN = "8755292870:AAGYFk5t_MddvDfN0lgZnhsDMTQYDxJI0Ps"

# الرابط الخاص بسيرفرك على Render
SERVER_URL = "https://linkedin-bot-9v0k.onrender.com" 

# 🖼️ رابط الصورة التوضيحية التي تظهر للجدد
START_IMAGE_URL = "https://i.imgur.com/example.jpg" 

# 📝 نص الشرح والترحيب بالعضو الجديد
START_TEXT = (
    "👋 <b>أهلاً بك في بوت تنظيم تفاعل LinkedIn!</b>\n\n"
    "📌 <b>كيف يعمل البوت؟</b>\n\n"
    "1️⃣ <b>النشر في الجروب:</b> أرسل رابط بوستك في الجروب بشكل طبيعي.\n"
    "2️⃣ <b>فحص الديون:</b> إذا كان عليك بوستات سابقة لم تتفاعل معها، سيطلب منك البوت التفاعل معها أولاً بالخاص.\n"
    "3️⃣ <b>التسجيل السريع:</b> يمكنك التفاعل مع أي بوست ينزل في الجروب بالضغط على زر <code>[ ✅ 2. سجّل تفاعلي ]</code> لخصمه من ديونك فوراً!\n"
    "4️⃣ <b>الحد اليومي:</b> يُسمح لكل عضو بنشر <b>بوست واحد فقط كل 24 ساعة</b>.\n\n"
    "🚀 <b>نتمنى لك توفيقاً وتفاعلاً رائعاً!</b>"
)

DAY_IN_SECONDS = 24 * 3600      # 24 ساعة
WEEK_IN_SECONDS = 7 * 24 * 3600 # 7 أيام

DATA_FILE = "bot_data.json"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- قاعدة البيانات والسجلات ---
active_posts = []           # قائمة البوستات
user_interactions = {}      # سجل تفاعلات كل عضو {user_id: set(post_ids_completed)}
user_verifications = {}      # حالات الفحص الحالية بالخاص
recent_group_opens = {}      # سجل فتح روابط الجروب {post_id: timestamp}

GROUP_CHAT_ID = None        # ايدي الجروب
BOT_USERNAME = None         # يوزر نيم البوت

def load_data():
    global active_posts, user_interactions, GROUP_CHAT_ID
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                active_posts = data.get("active_posts", [])
                user_interactions_raw = data.get("user_interactions", {})
                user_interactions = {int(k): set(v) for k, v in user_interactions_raw.items()}
                GROUP_CHAT_ID = data.get("group_chat_id", None)
        except Exception as e:
            logging.error(f"Error loading data: {e}")

def save_data():
    try:
        data = {
            "active_posts": active_posts,
            "user_interactions": {str(k): list(v) for k, v in user_interactions.items()},
            "group_chat_id": GROUP_CHAT_ID
        }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Error saving data: {e}")

async def delete_after(message, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass

# --- 1️⃣ دالة فحص الحياة (Health Check) لـ UptimeRobot ---
async def health_check(request):
    return web.Response(text="Bot is Alive!", status=200)

# --- 2️⃣ سيرفر التتبع السريع لروابط الجروب ---
async def handle_group_redirect(request):
    try:
        post_id = int(request.query.get("p", 0))
        target_post = next((p for p in active_posts if p["id"] == post_id), None)
        
        if target_post:
            recent_group_opens[post_id] = time.time()
            raise web.HTTPFound(target_post["link"])
    except web.HTTPFound as redirect:
        raise redirect
    except Exception as e:
        logging.error(f"Group redirect error: {e}")
    
    return web.Response(text="رابط غير صالح أو غير موجود", status=404)

# --- 3️⃣ سيرفر التحويل للخاص (لتدقيق الديون) ---
async def handle_redirect(request):
    try:
        user_id = int(request.query.get("u", 0))
        post_id = int(request.query.get("p", 0))
        
        if user_id in user_verifications:
            user_data = user_verifications[user_id]
            current_index = user_data["current_index"]
            uninteracted = user_data["uninteracted_posts"]
            
            if current_index < len(uninteracted):
                target_post = uninteracted[current_index]
                if target_post["id"] == post_id:
                    user_data["has_clicked_redirect"] = True
                    user_data["link_opened_time"] = time.time()
                    raise web.HTTPFound(target_post["link"])
    except web.HTTPFound as redirect:
        raise redirect
    except Exception as e:
        logging.error(f"Redirect error: {e}")
    
    return web.Response(text="رابط غير صالح أو انتهت الجلسة", status=400)

# --- 4️⃣ دالة نشر البوست في الجروب ---
async def publish_post_to_group(context: ContextTypes.DEFAULT_TYPE, post_id: int, user_name: str, link: str):
    if not GROUP_CHAT_ID:
        return

    safe_name = html.escape(user_name)
    tracking_link = f"{SERVER_URL}/gredirect?p={post_id}"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔗 1. افتح البوست", url=tracking_link),
            InlineKeyboardButton("✅ 2. سجّل تفاعلي", callback_data=f"gverify_{post_id}")
        ]
    ])

    text = (
        f"🚀 <b>بوست جديد من:</b> {safe_name}\n\n"
        f"⚠️ <b>طريقة التفاعل:</b> اضغط على <b>[ 🔗 1. افتح البوست ]</b> لتفتح البوست في لينكد إن وتعمل اللايك، ثم ارجع واضغط <b>[ ✅ 2. سجّل تفاعلي ]</b> لتسجيل نقطتك ⤵️"
    )

    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Error publishing post to group: {e}")

def get_verification_keyboard(user_id):
    data = user_verifications[user_id]
    current_index = data["current_index"]
    uninteracted = data["uninteracted_posts"]
    
    target_post = uninteracted[current_index]
    post_owner = html.escape(target_post["user"])
    post_id = target_post["id"]

    tracking_link = f"{SERVER_URL}/redirect?u={user_id}&p={post_id}"

    keyboard = [
        [InlineKeyboardButton(f"🔗 افتح بوست {post_owner} (خطوة {current_index + 1} من {len(uninteracted)})", url=tracking_link)],
        [InlineKeyboardButton(f"✅ تأكيد التفاعل", callback_data=f"verify_{post_id}")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def send_verification_step(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    data = user_verifications[user_id]
    current_index = data["current_index"]
    uninteracted = data["uninteracted_posts"]
    safe_name = html.escape(data["user_name"])

    text = (
        f"✋ أهلاً {safe_name}!\n"
        f"لتتمكن من نشر بوستك، يلزم التفاعل مع البوستات المتبقية عليك:\n\n"
        f"📌 <b>الخطوة ({current_index + 1} من {len(uninteracted)}):</b>\n"
        f"افتح الرابط التالي لعمل اللايك، ثم اضغط تأكيد."
    )
    
    return await context.bot.send_message(
        chat_id=user_id,
        text=text,
        reply_markup=get_verification_keyboard(user_id),
        parse_mode="HTML"
    )

# --- 5️⃣ أمر Start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id in user_verifications:
        await send_verification_step(context, user_id)
    else:
        try:
            await update.message.reply_photo(
                photo=START_IMAGE_URL,
                caption=START_TEXT,
                parse_mode="HTML"
            )
        except Exception:
            await update.message.reply_text(
                text=START_TEXT,
                parse_mode="HTML"
            )

# --- 6️⃣ معالجة الرسائل والروابط ---
async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global GROUP_CHAT_ID, BOT_USERNAME
    message = update.message
    if not message or not message.text:
        return

    if message.chat.type in ["group", "supergroup"]:
        if GROUP_CHAT_ID != message.chat_id:
            GROUP_CHAT_ID = message.chat_id
            save_data()

        if not BOT_USERNAME:
            bot_info = await context.bot.get_me()
            BOT_USERNAME = bot_info.username

        text = message.text
        user_id = message.from_user.id
        user_name = message.from_user.first_name or "عضو"
        safe_name = html.escape(user_name)

        linkedin_pattern = r"(https?://[^\s]*(?:linkedin\.com|lnkd\.in)[^\s]*)"
        matches = re.findall(linkedin_pattern, text, re.IGNORECASE)

        # 🚫 مسح أي كلام غير روابط LinkedIn
        if not matches:
            try:
                await message.delete()
            except Exception:
                pass

            try:
                warn_msg = await context.bot.send_message(
                    chat_id=message.chat_id,
                    text=f"⚠️ يا {safe_name}، يُسمح فقط بنشر <b>روابط LinkedIn</b> في هذا الجروب!\nتم مسح رسالتك تلقائياً.",
                    parse_mode="HTML"
                )
                asyncio.create_task(delete_after(warn_msg, 10))
            except Exception:
                pass
            return

        valid_link = matches[0]
        now = time.time()

        # 🔒 فحص الحد اليومي (بوست كل 24 ساعة)
        user_posts = [p for p in active_posts if p["user_id"] == user_id]
        if user_posts:
            last_post = max(user_posts, key=lambda x: x["timestamp"])
            time_passed = now - last_post["timestamp"]

            if time_passed < DAY_IN_SECONDS:
                remaining_seconds = DAY_IN_SECONDS - time_passed
                hours = int(remaining_seconds // 3600)
                minutes = int((remaining_seconds % 3600) // 60)

                try:
                    await message.delete()
                except Exception:
                    pass

                time_msg = f"⏳ <b>المتبقي لتتمكن من النشر مجدداً:</b> {hours} ساعة و {minutes} دقيقة."

                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"⚠️ <b>عذراً يا {safe_name}!</b>\n"
                             f"يُسمح بنشر <b>بوست واحد فقط كل 24 ساعة</b>.\n\n"
                             f"{time_msg}",
                        parse_mode="HTML"
                    )
                except Forbidden:
                    temp_msg = await context.bot.send_message(
                        chat_id=message.chat_id,
                        text=f"⚠️ يا {safe_name}، يُسمح بنشر بوست واحد فقط كل 24 ساعة!\n{time_msg}",
                        parse_mode="HTML"
                    )
                    asyncio.create_task(delete_after(temp_msg, 15))
                return

        try:
            await message.delete()
        except Exception:
            pass

        completed_set = user_interactions.get(user_id, set())

        uninteracted_posts = [
            p for p in active_posts 
            if p["user_id"] != user_id 
            and p["id"] not in completed_set
            and (now - p.get("timestamp", 0)) <= WEEK_IN_SECONDS
        ]

        if len(uninteracted_posts) == 0:
            new_post_id = len(active_posts) + 1
            new_post = {
                "id": new_post_id,
                "user": user_name,
                "user_id": user_id,
                "link": valid_link,
                "timestamp": now
            }
            active_posts.append(new_post)
            save_data()

            await publish_post_to_group(context, new_post_id, user_name, valid_link)

            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎉 <b>عاش يا {safe_name}!</b> لأنك متفاعل مع جميع البوستات المطلوبة، تم نشر بوستك فوراً في الجروب! 🚀",
                    parse_mode="HTML"
                )
            except Forbidden:
                pass
            return

        user_verifications[user_id] = {
            "pending_link": valid_link,
            "user_name": user_name,
            "uninteracted_posts": uninteracted_posts,
            "current_index": 0,
            "has_clicked_redirect": False,
            "link_opened_time": 0
        }

        try:
            await send_verification_step(context, user_id)
        except Forbidden:
            btn = InlineKeyboardMarkup([[
                InlineKeyboardButton("📩 اضغط هنا لبدء الفحص بالخاص", url=f"https://t.me/{BOT_USERNAME}?start=verify")
            ]])
            temp_msg = await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"⚠️ يا {safe_name}، يرجى الضغط على الزر بالأسفل لبدء فحص التفاعل بالخاص ونشر بوستك!",
                reply_markup=btn,
                parse_mode="HTML"
            )
            asyncio.create_task(delete_after(temp_msg, 20))

# --- 7️⃣ معالجة الأزرار ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    data = query.data

    # ✅ زر تسجيل التفاعل السريع من الجروب
    if data.startswith("gverify_"):
        try:
            post_id = int(data.split("_")[1])
        except ValueError:
            return

        target_post = next((p for p in active_posts if p["id"] == post_id), None)

        if not target_post:
            try:
                await query.answer("⚠️ البوست غير موجود أو قديم جداً.", show_alert=True)
            except Exception:
                pass
            return

        if target_post["user_id"] == user_id:
            try:
                await query.answer("😊 ده بوستك أنت يا بطل! مش محتاج تسجل تفاعلك معاه.", show_alert=True)
            except Exception:
                pass
            return

        # 🛑 التحقق من فتح رابط البوست أولاً
        open_time = recent_group_opens.get(post_id, 0)
        if not open_time or (time.time() - open_time > 600):  # نافذة زمنية مدتها 10 دقائق
            try:
                await query.answer(
                    "❌ عذراً! يجب الضغط على زر [ 🔗 1. افتح البوست ] والتفاعل معه أولاً قبل تسجيل نقطتك!",
                    show_alert=True
                )
            except Exception:
                pass
            return

        if user_id not in user_interactions:
            user_interactions[user_id] = set()

        if post_id in user_interactions[user_id]:
            try:
                await query.answer("⚠️ أنت بالفعل مسجّل تفاعلك مع البوست ده قبل كدا!", show_alert=True)
            except Exception:
                pass
            return

        user_interactions[user_id].add(post_id)
        save_data()

        owner_name = html.escape(target_post["user"])
        try:
            await query.answer(
                f"✅ تم تسجيل تفاعلك مع بوست {owner_name} بنجاح! 👏\n\n"
                f"اتخصم البوست ده من ديونك، ولما تنزل بوستك هينزل أسرع بدون مشاكل!",
                show_alert=True
            )
        except Exception:
            pass
        return

    # 3. خطوات الفحص بالخاص
    if user_id not in user_verifications:
        try:
            await query.answer("⚠️ ليس لديك عملية فحص معلقة حالياً.", show_alert=True)
        except Exception:
            pass
        return

    user_data = user_verifications[user_id]
    current_index = user_data["current_index"]
    uninteracted = user_data["uninteracted_posts"]

    if current_index >= len(uninteracted):
        try:
            await query.answer("✅ تمت الفحوصات بالفعل!", show_alert=True)
        except Exception:
            pass
        return

    current_post = uninteracted[current_index]

    if data.startswith("verify_"):
        if not user_data.get("has_clicked_redirect", False):
            try:
                await query.answer(
                    "❌ لم يتم فتح الرابط!\n\nيجب الضغط على زرار (🔗 افتح بوست) والتأكد من تحويلك للصفحة أولاً.",
                    show_alert=True
                )
            except Exception:
                pass
            return

        elapsed_time = time.time() - user_data.get("link_opened_time", 0)
        if elapsed_time < 2:
            try:
                await query.answer(
                    "⏳ يرجى الانتظار ثانيتين والتأكد من فتح الرابط والتفاعل معه أولاً.",
                    show_alert=True
                )
            except Exception:
                pass
            return

        if user_id not in user_interactions:
            user_interactions[user_id] = set()
        user_interactions[user_id].add(current_post["id"])

        user_data["current_index"] += 1
        user_data["has_clicked_redirect"] = False

        safe_user_name = html.escape(user_data["user_name"])

        if user_data["current_index"] >= len(uninteracted):
            pending_link = user_data["pending_link"]
            user_name = user_data["user_name"]
            now = time.time()

            new_post_id = len(active_posts) + 1
            active_posts.append({
                "id": new_post_id,
                "user": user_name,
                "user_id": user_id,
                "link": pending_link,
                "timestamp": now
            })
            save_data()

            await publish_post_to_group(context, new_post_id, user_name, pending_link)

            try:
                await query.message.edit_text(
                    f"🎉 <b>تم بنجاح يا {safe_user_name}!</b>\n\n"
                    f"✅ اجتزت فحص جميع البوستات المطلوبة، وتم نشر بوستك الآن فوراً في الجروب! 🚀",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Error editing message on completion: {e}")

            try:
                await query.answer("🎉 تم نشر بوستك بنجاح!")
            except Exception:
                pass

            del user_verifications[user_id]
        else:
            next_index = user_data["current_index"]
            try:
                await query.answer(f"✅ تم تأكيد التفاعل للبوست ({next_index} من {len(uninteracted)})!")
            except Exception:
                pass
            
            target_next = uninteracted[next_index]
            post_owner = html.escape(target_next["user"])
            post_id = target_next["id"]
            tracking_link = f"{SERVER_URL}/redirect?u={user_id}&p={post_id}"

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🔗 افتح بوست {post_owner} (خطوة {next_index + 1} من {len(uninteracted)})", url=tracking_link)],
                [InlineKeyboardButton(f"✅ تأكيد التفاعل", callback_data=f"verify_{post_id}")]
            ])

            try:
                await query.message.edit_text(
                    text=f"✋ استمر يا {safe_user_name}!\n\n"
                         f"📌 <b>الخطوة ({next_index + 1} من {len(uninteracted)}):</b>\n"
                         f"افتح الرابط التالي لعمل اللايك ثم اضغط تأكيد.",
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Error editing message on step update: {e}")

# --- 8️⃣ دالة التشغيل الرئيسية ---
async def main():
    load_data()

    web_app = web.Application()
    web_app.router.add_get('/', health_check)
    web_app.router.add_get('/redirect', handle_redirect)
    web_app.router.add_get('/gredirect', handle_group_redirect)
    runner = web.AppRunner(web_app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    app = ApplicationBuilder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), check_message))
    app.add_handler(CallbackQueryHandler(button_handler))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await asyncio.Event().wait()

if __name__ == '__main__':
    asyncio.run(main())
